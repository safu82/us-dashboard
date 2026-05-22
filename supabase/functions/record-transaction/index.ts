import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

// Secure write path for new transactions (manual entry now; Citi ingestion
// later). RLS blocks anon writes, so this function is the only way to add a
// transaction. Auth is a shared write key (env RECORD_TXN_KEY) — verify_jwt is
// disabled because this function implements its own auth.
//
// FIFO lot accounting is NOT done here — it is derived client-side in the
// dashboard from the frozen holding_lots seed + transactions. This function
// only records the trade and keeps the holdings table current (quantity and
// weighted-average cost; avg_cost is just the snapshot-fallback price).

const WRITE_KEY = Deno.env.get('RECORD_TXN_KEY') || '';

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: CORS });
  try {
    if (req.method !== 'POST') return json({ error: 'POST only' }, 405);

    const body = await req.json().catch(() => ({}));
    if (!WRITE_KEY || body.key !== WRITE_KEY) {
      return json({ error: 'unauthorized' }, 401);
    }

    const date = String(body.date || '');
    const type = String(body.type || '').toUpperCase();
    const ticker = String(body.ticker || '').toUpperCase().trim();
    const stock = String(body.stock || ticker).trim();
    const quantity = Number(body.quantity);
    const price = Number(body.price);

    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return json({ error: 'date must be YYYY-MM-DD' }, 400);
    if (type !== 'BUY' && type !== 'SELL') return json({ error: 'type must be BUY or SELL' }, 400);
    if (!ticker) return json({ error: 'ticker required' }, 400);
    if (!(quantity > 0)) return json({ error: 'quantity must be positive' }, 400);
    if (!(price > 0)) return json({ error: 'price must be positive' }, 400);

    const supabase = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? '',
    );

    const { data: holding } = await supabase
      .from('holdings').select('*').eq('ticker', ticker).maybeSingle();
    const heldQty = holding ? Number(holding.quantity) : 0;

    if (type === 'SELL' && quantity > heldQty) {
      return json({ error: `cannot sell ${quantity} — only ${heldQty} ${ticker} held` }, 400);
    }

    // 1. Record the transaction.
    const { data: txn, error: txnErr } = await supabase
      .from('transactions')
      .insert({ date, type, ticker, stock, quantity, price })
      .select('id').single();
    if (txnErr) throw txnErr;

    // 2. Keep holdings current.
    if (type === 'BUY') {
      if (holding) {
        const newQty = heldQty + quantity;
        const newAvg = (heldQty * Number(holding.avg_cost) + quantity * price) / newQty;
        await supabase.from('holdings')
          .update({ quantity: newQty, avg_cost: newAvg, updated_at: new Date().toISOString() })
          .eq('ticker', ticker);
      } else {
        // New position — pull the sector from the canonical GICS map.
        const { data: sec } = await supabase
          .from('us_stock_sectors').select('sector').eq('ticker', ticker).maybeSingle();
        await supabase.from('holdings').insert({
          ticker, name: stock, quantity, avg_cost: price,
          sector: sec?.sector || 'Unknown',
        });
      }
    } else { // SELL
      const newQty = heldQty - quantity;
      if (newQty > 0) {
        await supabase.from('holdings')
          .update({ quantity: newQty, updated_at: new Date().toISOString() })
          .eq('ticker', ticker);
      } else {
        await supabase.from('holdings').delete().eq('ticker', ticker);
      }
    }

    return json({ success: true, transaction_id: txn.id, ticker, type, quantity, price });
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : String(e) }, 500);
  }
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { ...CORS, 'Content-Type': 'application/json' },
  });
}

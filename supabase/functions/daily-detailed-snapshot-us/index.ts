import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

// EOD writer for detailed_snapshots — enriched per-holding rows + index levels.
// Port of the India daily-detailed-snapshot-india.ts (no portfolio column;
// US indices; reads holdings x live_prices, falls back to Yahoo for indices).

Deno.serve(async () => {
  try {
    const supabase = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? '',
    );

    const etDate = new Date().toLocaleDateString('en-US', {
      timeZone: 'America/New_York',
      year: 'numeric', month: '2-digit', day: '2-digit',
    });
    const [month, day, year] = etDate.split('/');
    const today = `${year}-${month}-${day}`;
    console.log(`Capturing US detailed snapshot for ${today}`);

    const { data: holdings, error: holdErr } = await supabase
      .from('holdings').select('*').gt('quantity', 0);
    if (holdErr) throw new Error(`holdings: ${holdErr.message}`);
    if (!holdings?.length) throw new Error('No holdings found');

    const { data: livePrices, error: priceErr } = await supabase
      .from('live_prices').select('ticker, price');
    if (priceErr) throw new Error(`live_prices: ${priceErr.message}`);
    const prices: Record<string, number> = {};
    livePrices?.forEach((r) => { if (r.price > 0) prices[r.ticker] = r.price; });

    // Indices — prefer live_prices, fall back to Yahoo.
    const indices = {
      SP500: prices['^GSPC'] || await fetchYahooPrice('^GSPC'),
      NASDAQ: prices['^IXIC'] || await fetchYahooPrice('^IXIC'),
      DOW: prices['^DJI'] || await fetchYahooPrice('^DJI'),
      QQQ: prices['QQQ'] || await fetchYahooPrice('QQQ'),
    };

    const enriched = holdings.map((h) => {
      const quantity = h.quantity || 0;
      const avgCost = h.avg_cost || 0;
      const currentPrice = prices[h.ticker] || 0;
      const value = quantity * currentPrice;
      const costValue = quantity * avgCost;
      const unrealizedPL = value - costValue;
      return {
        ticker: h.ticker,
        name: h.name || '',
        quantity,
        avgCost,
        currentPrice,
        value: round2(value),
        unrealizedPL: round2(unrealizedPL),
        unrealizedPLPercent: costValue > 0 ? round2((unrealizedPL / costValue) * 100) : 0,
        sector: h.sector || '',
      };
    });

    const totalValue = enriched.reduce((sum, h) => sum + h.value, 0);

    const { error: upErr } = await supabase
      .from('detailed_snapshots')
      .upsert({
        snapshot_date: today,
        total_value: round2(totalValue),
        holdings: enriched,
        indices,
      }, { onConflict: 'snapshot_date' });
    if (upErr) throw upErr;

    console.log(`Stored detailed snapshot ${today}: $${totalValue.toFixed(2)}`);
    return json({
      success: true, date: today,
      total_value: round2(totalValue), total_stocks: enriched.length,
    });
  } catch (e) {
    console.error('Error:', e);
    return json({ error: e instanceof Error ? e.message : String(e) }, 500);
  }
});

async function fetchYahooPrice(symbol: string): Promise<number> {
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?interval=1d&range=1d`;
    const res = await fetch(url);
    const data = await res.json();
    return data.chart?.result?.[0]?.meta?.regularMarketPrice || 0;
  } catch (e) {
    console.error(`Yahoo fallback failed for ${symbol}:`, e);
    return 0;
  }
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

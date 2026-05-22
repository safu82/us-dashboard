import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

// EOD writer for portfolio_snapshots — total NAV + QQQ benchmark.
// Reads holdings x live_prices; falls back to avg_cost when a price is missing.
// Port of the India India-daily-snapshot.ts (no portfolio column; QQQ benchmark).

Deno.serve(async () => {
  try {
    const supabase = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? '',
    );

    // Snapshot date in US Eastern time.
    const etDate = new Date().toLocaleDateString('en-US', {
      timeZone: 'America/New_York',
      year: 'numeric', month: '2-digit', day: '2-digit',
    });
    const [month, day, year] = etDate.split('/');
    const snapshotDate = `${year}-${month}-${day}`;
    console.log(`Snapshot date (ET): ${snapshotDate}`);

    const { data: existing } = await supabase
      .from('portfolio_snapshots')
      .select('id')
      .eq('snapshot_date', snapshotDate)
      .maybeSingle();
    if (existing) {
      return json({ success: true, message: 'Snapshot already exists', date: snapshotDate });
    }

    // Live prices.
    const { data: livePrices, error: pricesErr } = await supabase
      .from('live_prices').select('ticker, price, updated_at');
    if (pricesErr) throw pricesErr;

    // Staleness guard — refuse to snapshot off stale prices (e.g. if the
    // Railway worker was down today). A missing day is recoverable; a wrong
    // NAV row silently corrupts the equity curve and XIRR.
    let freshestMs = 0;
    livePrices?.forEach((r) => {
      const t = Date.parse(r.updated_at);
      if (t > freshestMs) freshestMs = t;
    });
    const freshestEt = new Date(freshestMs).toLocaleDateString('en-US', {
      timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    });
    const [fM, fD, fY] = freshestEt.split('/');
    if (`${fY}-${fM}-${fD}` !== snapshotDate) {
      console.warn(`Stale live_prices (latest ${fY}-${fM}-${fD}) — skipping snapshot`);
      return json({
        success: false, skipped: true,
        reason: 'live_prices is stale — the live-price worker may be down',
        latest_price_date: `${fY}-${fM}-${fD}`, snapshot_date: snapshotDate,
      });
    }

    const priceMap: Record<string, number> = {};
    livePrices?.forEach((r) => { if (r.price > 0) priceMap[r.ticker] = r.price; });

    // Holdings.
    const { data: holdings, error: holdErr } = await supabase
      .from('holdings').select('ticker, quantity, avg_cost').gt('quantity', 0);
    if (holdErr) throw holdErr;

    let totalValue = 0;
    const missing: string[] = [];
    holdings?.forEach((h) => {
      const price = priceMap[h.ticker] ?? h.avg_cost;
      if (!priceMap[h.ticker]) missing.push(h.ticker);
      totalValue += h.quantity * price;
    });

    // QQQ benchmark CAGR — baseline $268.65 on 2023-01-02.
    const qqqPrice = priceMap['QQQ'] || 0;
    const qqqStartPrice = 268.65;
    const qqqStartDate = new Date('2023-01-02');
    const years = (Date.now() - qqqStartDate.getTime()) / (1000 * 60 * 60 * 24 * 365.25);
    const qqqXIRR = qqqPrice > 0
      ? (Math.pow(qqqPrice / qqqStartPrice, 1 / years) - 1) * 100
      : 0;

    const { error: insErr } = await supabase.from('portfolio_snapshots').insert({
      snapshot_date: snapshotDate,
      total_value: totalValue,
      benchmark_ticker: 'QQQ',
      benchmark_price: qqqPrice,
      benchmark_xirr: qqqXIRR,
    });
    if (insErr) throw insErr;

    console.log(`Saved snapshot ${snapshotDate}: $${totalValue.toFixed(2)}`);
    return json({
      success: true, date: snapshotDate, total_value: totalValue,
      benchmark_xirr: qqqXIRR, missing_prices: missing,
    });
  } catch (e) {
    console.error('Error:', e);
    return json({ success: false, error: String(e) }, 500);
  }
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

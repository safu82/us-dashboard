import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

// Schedule: 1:00 AM IST (7:30 PM UTC previous day) — after US market close

const US_PRICES_URL = 'https://docs.google.com/spreadsheets/d/e/2PACX-1vQpKobYC_r4HJcUca6Q5XIXLYobdLLzksOxaScAMGMAqRE41K-yu-LRjz7azM-lM2tL2OKV82E9_Omj/pub?output=csv';

Deno.serve(async (req) => {
  try {
    const supabase = createClient(
      Deno.env.get('SUPABASE_URL') ?? '',
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    );

    const today = new Date().toISOString().split('T')[0];
    console.log(`📅 Capturing US portfolio snapshot for ${today}...`);

    // ── 1. Holdings ─────────────────────────────────────────────────────────
    const { data: holdings, error: holdingsError } = await supabase
      .from('holdings')
      .select('*')
      .eq('portfolio', 'US')
      .gt('quantity', 0);

    if (holdingsError) throw new Error(`Failed to fetch holdings: ${holdingsError.message}`);
    if (!holdings?.length) throw new Error('No holdings found for US portfolio');
    console.log(`✅ Found ${holdings.length} holdings`);

    // ── 2. Prices from Google Sheets ────────────────────────────────────────
    const prices = await fetchPricesFromSheet(US_PRICES_URL);
    console.log(`✅ Fetched ${Object.keys(prices).length} prices from Google Sheets`);

    // ── 3. Indices — prefer sheet prices (already fetched), fall back to Yahoo ─
    const indices = {
      SP500:  prices['SPY'] || prices['^GSPC'] || await fetchYahooPrice('^GSPC'),
      QQQ:    prices['QQQ'] || await fetchYahooPrice('QQQ'),
      NASDAQ: prices['QQQ'] || prices['^IXIC'] || await fetchYahooPrice('^IXIC')
    };

    // ── 4. Build enriched holdings ───────────────────────────────────────────
    const enrichedHoldings = holdings.map((h) => {
      const quantity     = h.quantity    || 0;
      const avgCost      = h.avg_cost    || 0;
      const currentPrice = prices[h.ticker] || 0;
      const value        = quantity * currentPrice;
      const costValue    = quantity * avgCost;
      const unrealizedPL = value - costValue;

      return {
        ticker:               h.ticker,
        name:                 h.name || '',
        quantity,
        avgCost,
        currentPrice,
        value:                Math.round(value * 100) / 100,
        unrealizedPL:         Math.round(unrealizedPL * 100) / 100,
        unrealizedPLPercent:  costValue > 0
          ? Math.round((unrealizedPL / costValue * 100) * 100) / 100
          : 0,
        sector: h.sector || ''
      };
    });

    const totalValue = enrichedHoldings.reduce((sum, h) => sum + h.value, 0);
    console.log(`💰 Total value: $${(totalValue / 1000).toFixed(1)}K`);

    // ── 5. Store snapshot ────────────────────────────────────────────────────
    const { error: upsertError } = await supabase
      .from('detailed_snapshots')
      .upsert({
        portfolio:    'US',
        snapshot_date: today,
        total_value:  Math.round(totalValue * 100) / 100,
        holdings:     enrichedHoldings,
        indices
      }, { onConflict: 'portfolio,snapshot_date' });

    if (upsertError) throw upsertError;
    console.log('✅ US snapshot stored successfully');

    return new Response(JSON.stringify({
      success:      true,
      date:         today,
      total_value:  Math.round(totalValue * 100) / 100,
      total_stocks: enrichedHoldings.length,
      source:       'Google Sheets'
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });

  } catch (error) {
    console.error('❌ Error:', error);
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
});

// ── Ticker mapping: Google Sheets format → Yahoo/holding ticker ───────────
async function fetchPricesFromSheet(sheetUrl: string): Promise<Record<string, number>> {
  const response = await fetch(sheetUrl);
  const text = await response.text();
  const prices: Record<string, number> = {};

  for (const line of text.trim().split('\n')) {
    const parts = line.split(',').map((p) => p.trim().replace(/"/g, ''));
    if (parts.length < 2) continue;

    let ticker = parts[0];
    const price = parseFloat(parts[1].replace(/,/g, ''));
    if (isNaN(price) || price <= 0) continue;

    // Normalise exchange-prefixed tickers from Google Finance format
    if (ticker.includes(':')) {
      const [exchange, symbol] = ticker.split(':');
      if (ticker === 'BOM:517431')        ticker = 'NARMP.BO';
      else if (ticker === 'NSE:KPEL')     ticker = 'KPENERGY.NS';
      else if (exchange === 'NSE')        ticker = symbol + '.NS';
      else                                ticker = symbol;  // NASDAQ / NYSE
    }

    prices[ticker] = price;
  }

  return prices;
}

async function fetchYahooPrice(symbol: string): Promise<number> {
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?interval=1d&range=1d`;
    const response = await fetch(url);
    const data = await response.json();
    return data.chart?.result?.[0]?.meta?.regularMarketPrice || 0;
  } catch (e) {
    console.error(`⚠️ Yahoo price failed for ${symbol}:`, e);
    return 0;
  }
}

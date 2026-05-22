import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

// US Portfolio EOD Telegram alert.
// Port of the India India-Portfolio-alert.ts: reads the two most recent
// detailed_snapshots, computes per-stock day change on the CURRENT quantity
// (so a partial sell isn't misread as a loss), nets today's transactions,
// and posts an HTML summary via the Telegram Bot API.

const TELEGRAM_BOT_TOKEN = Deno.env.get('TELEGRAM_BOT_TOKEN') || '';
const TELEGRAM_CHAT_ID = Deno.env.get('TELEGRAM_CHAT_ID') || '';
const SUPABASE_URL = Deno.env.get('SUPABASE_URL') || '';
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') || '';

interface StockData {
  ticker: string;
  name: string;
  change_pct: number;
  day_change_amount: number;
  value: number;
}

interface Metrics {
  total_value: number;
  total_day_change: number;
  day_change_percent: number;
  biggest_gainer: { name: string; percent: number };
  biggest_loser: { name: string; percent: number };
  total_stocks: number;
  today_buy_value: number;
  today_sell_value: number;
}

async function fetchPortfolio(): Promise<{ stocks: StockData[]; netCashFlow: number }> {
  if (!SUPABASE_URL || !SUPABASE_SERVICE_ROLE_KEY) {
    console.error('Missing Supabase credentials');
    return { stocks: [], netCashFlow: 0 };
  }
  const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

  // Latest snapshot.
  const { data: latest, error: latestErr } = await supabase
    .from('detailed_snapshots')
    .select('snapshot_date, holdings')
    .order('snapshot_date', { ascending: false })
    .limit(1)
    .single();
  if (latestErr || !latest?.holdings) {
    console.error('Error fetching latest snapshot:', latestErr);
    return { stocks: [], netCashFlow: 0 };
  }
  const snapshotDate = latest.snapshot_date;
  const portfolioData = Array.isArray(latest.holdings)
    ? latest.holdings : JSON.parse(latest.holdings);

  // Previous snapshot.
  const { data: prev } = await supabase
    .from('detailed_snapshots')
    .select('snapshot_date, holdings')
    .lt('snapshot_date', snapshotDate)
    .order('snapshot_date', { ascending: false })
    .limit(1)
    .single();
  const prevMap: Record<string, any> = {};
  if (prev?.holdings) {
    const prevData = Array.isArray(prev.holdings) ? prev.holdings : JSON.parse(prev.holdings);
    prevData.forEach((s: any) => { prevMap[s.ticker] = s; });
  } else {
    console.warn('No previous snapshot — changes will be 0%');
  }

  // Today's transactions (so a partial sell isn't read as a loss).
  const { data: todayTxns } = await supabase
    .from('transactions')
    .select('type, ticker, quantity, price')
    .eq('date', snapshotDate);
  let todayBuyValue = 0;
  let todaySellValue = 0;
  for (const t of (todayTxns || [])) {
    const value = t.quantity * t.price;
    if (t.type === 'BUY') todayBuyValue += value;
    if (t.type === 'SELL') todaySellValue += value;
  }
  const netCashFlow = todaySellValue - todayBuyValue;

  // Per-stock day change on the CURRENT quantity (pure price move).
  const stocks: StockData[] = portfolioData.map((stock: any) => {
    const prevStock = prevMap[stock.ticker];
    let dayChangeAmount = 0;
    let changePct = 0;
    if (prevStock && prevStock.quantity > 0) {
      const prevPricePerShare = prevStock.value / prevStock.quantity;
      const prevValueAtCurrentQty = prevPricePerShare * stock.quantity;
      dayChangeAmount = stock.value - prevValueAtCurrentQty;
      changePct = prevValueAtCurrentQty > 0
        ? (dayChangeAmount / prevValueAtCurrentQty) * 100
        : 0;
    }
    return {
      ticker: stock.ticker,
      name: stock.name,
      change_pct: changePct,
      day_change_amount: dayChangeAmount,
      value: stock.value,
    };
  });

  return { stocks, netCashFlow };
}

function calculateMetrics(stocks: StockData[], netCashFlow: number): Metrics | null {
  if (!stocks.length) return null;
  const totalValue = stocks.reduce((s, x) => s + x.value, 0);
  const totalDayChange = stocks.reduce((s, x) => s + x.day_change_amount, 0);
  const prevComparable = totalValue - totalDayChange;
  const dayChangePercent = prevComparable > 0 ? (totalDayChange / prevComparable) * 100 : 0;
  const biggestGainer = stocks.reduce((max, s) => s.change_pct > max.change_pct ? s : max);
  const biggestLoser = stocks.reduce((min, s) => s.change_pct < min.change_pct ? s : min);
  return {
    total_value: totalValue,
    total_day_change: totalDayChange,
    day_change_percent: dayChangePercent,
    biggest_gainer: { name: biggestGainer.name, percent: biggestGainer.change_pct },
    biggest_loser: { name: biggestLoser.name, percent: biggestLoser.change_pct },
    total_stocks: stocks.length,
    today_buy_value: netCashFlow < 0 ? Math.abs(netCashFlow) : 0,
    today_sell_value: netCashFlow > 0 ? netCashFlow : 0,
  };
}

function formatCurrency(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(2)}K`;
  return `${sign}$${abs.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
}

function formatTelegramMessage(m: Metrics): string {
  const today = new Date().toLocaleDateString('en-US', {
    day: '2-digit', month: 'short', year: 'numeric', weekday: 'long',
    timeZone: 'America/New_York',
  });
  const emoji = m.total_day_change >= 0 ? '📈' : '📉';
  const sign = m.total_day_change >= 0 ? '+' : '';

  let txnNote = '';
  if (m.today_sell_value > 0 || m.today_buy_value > 0) {
    const parts: string[] = [];
    if (m.today_sell_value > 0) parts.push(`Sold ${formatCurrency(m.today_sell_value)}`);
    if (m.today_buy_value > 0) parts.push(`Bought ${formatCurrency(m.today_buy_value)}`);
    txnNote = `\n💼 <i>Today's trades: ${parts.join(' | ')}</i>`;
  }

  return `
<b>🇺🇸 US Portfolio EOD Update</b>
<b>${today}</b>

<b>💼 Portfolio Value:</b> ${formatCurrency(m.total_value)}

<b>${emoji} Day's Change:</b> ${sign}${formatCurrency(m.total_day_change)} (${sign}${m.day_change_percent.toFixed(2)}%)${txnNote}

<b>🚀 Biggest Gainer:</b>
${m.biggest_gainer.name}: <b>+${m.biggest_gainer.percent.toFixed(2)}%</b>

<b>📉 Biggest Loser:</b>
${m.biggest_loser.name}: <b>${m.biggest_loser.percent.toFixed(2)}%</b>

<i>Total Stocks: ${m.total_stocks}</i>
`.trim();
}

async function sendTelegramMessage(message: string): Promise<boolean> {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
    console.error('Telegram secrets missing — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID');
    return false;
  }
  try {
    const res = await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: TELEGRAM_CHAT_ID, text: message, parse_mode: 'HTML' }),
    });
    if (res.ok) return true;
    console.error('Telegram API error:', await res.text());
    return false;
  } catch (e) {
    console.error('Error sending Telegram message:', e);
    return false;
  }
}

Deno.serve(async () => {
  try {
    const { stocks, netCashFlow } = await fetchPortfolio();
    if (!stocks.length) {
      return json({ error: 'No portfolio data fetched' }, 500);
    }
    const metrics = calculateMetrics(stocks, netCashFlow);
    if (!metrics) {
      return json({ error: 'Failed to calculate metrics' }, 500);
    }
    const sent = await sendTelegramMessage(formatTelegramMessage(metrics));
    return json({ success: sent, metrics }, sent ? 200 : 500);
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : String(e) }, 500);
  }
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

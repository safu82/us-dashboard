import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

// ============ CONFIGURATION ============
const TELEGRAM_BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN") || "";
const TELEGRAM_CHAT_ID = Deno.env.get("TELEGRAM_CHAT_ID") || "";
const GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQpKobYC_r4HJcUca6Q5XIXLYobdLLzksOxaScAMGMAqRE41K-yu-LRjz7azM-lM2tL2OKV82E9_Omj/pub?output=csv";
const PERIOD_START_VALUE = 683802.0; // Sept 30, 2025

function parseNumber(valueStr: string): number {
  if (!valueStr) return 0;
  try {
    return parseFloat(valueStr.replace(/,/g, ''));
  } catch {
    return 0;
  }
}

async function fetchGoogleSheetsData() {
  try {
    const response = await fetch(GOOGLE_SHEET_URL);
    const csvText = await response.text();
    const lines = csvText.trim().split('\n');
    
    const portfolioData: any[] = [];
    
    for (const line of lines) {
      // Parse CSV line (handle quoted values)
      const row = line.split(',').map(cell => cell.trim().replace(/^"|"$/g, ''));
      
      if (row.length < 5 || !row[0] || !row[1]) continue;
      
      const symbol = row[0].trim();
      
      // Extract ticker from symbol (remove exchange prefix if any)
      let ticker: string;
      if (symbol.includes(':')) {
        ticker = symbol.split(':')[1].trim();
      } else {
        ticker = symbol.trim();
      }
      
      try {
        const price = parseNumber(row[1]);
        const change = parseNumber(row[2]);
        const quantity = parseNumber(row[3]);
        const dayChangeAmount = row[4] ? parseNumber(row[4]) : 0;
        
        // Skip if invalid data
        if (price === 0 && quantity === 0) continue;
        
        // Calculate change percentage
        const prevClose = price - change;
        const changePct = prevClose > 0 ? (change / prevClose * 100) : 0;
        
        portfolioData.push({
          ticker,
          name: ticker,
          price,
          quantity,
          change_pct: changePct,
          day_change_amount: dayChangeAmount,
          value: price * quantity
        });
      } catch (e) {
        console.error(`Error parsing ${symbol}:`, e);
        continue;
      }
    }
    
    console.log(`✅ Fetched ${portfolioData.length} US stocks from Google Sheets`);
    return portfolioData;
  } catch (e) {
    console.error('❌ Error fetching Google Sheets:', e);
    return [];
  }
}

function calculateMetrics(portfolioData: any[]) {
  if (!portfolioData.length) return null;
  
  const totalValue = portfolioData.reduce((sum, stock) => sum + stock.value, 0);
  const totalDayChange = portfolioData.reduce((sum, stock) => sum + stock.day_change_amount, 0);
  
  const biggestGainer = portfolioData.reduce((max, stock) => 
    stock.change_pct > max.change_pct ? stock : max
  );
  
  const biggestLoser = portfolioData.reduce((min, stock) => 
    stock.change_pct < min.change_pct ? stock : min
  );
  
  const dayChangePercent = totalValue ? (totalDayChange / (totalValue - totalDayChange) * 100) : 0;
  
  return {
    total_value: totalValue,
    total_day_change: totalDayChange,
    day_change_percent: dayChangePercent,
    biggest_gainer: {
      name: biggestGainer.name,
      percent: biggestGainer.change_pct
    },
    biggest_loser: {
      name: biggestLoser.name,
      percent: biggestLoser.change_pct
    },
    total_stocks: portfolioData.length
  };
}

async function sendTelegramMessage(message: string) {
  if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
    console.error("❌ Telegram secrets missing. Did you run `supabase secrets set`?");
    return false;
  }
  
  try {
    const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: TELEGRAM_CHAT_ID,
        text: message,
        parse_mode: 'HTML'
      })
    });
    
    if (response.ok) {
      console.log('✅ Telegram message sent successfully');
      return true;
    } else {
      const errorText = await response.text();
      console.error('❌ Telegram API error:', errorText);
      return false;
    }
  } catch (e) {
    console.error('❌ Error sending Telegram message:', e);
    return false;
  }
}

function formatCurrency(value: number): string {
  if (Math.abs(value) >= 1000000) {
    return `${(value / 1000000).toFixed(2)}M`;
  } else if (Math.abs(value) >= 1000) {
    return `${(value / 1000).toFixed(2)}K`;
  } else {
    return `${value.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  }
}

function formatTelegramMessage(metrics: any): string {
  const today = new Date().toLocaleDateString('en-US', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    weekday: 'long',
    timeZone: 'America/New_York'
  });
  
  const dayChangeEmoji = metrics.total_day_change >= 0 ? '📈' : '📉';
  const dayChangeSign = metrics.total_day_change >= 0 ? '+' : '';
  
  return `
<b>🇺🇸 US Portfolio EOD Update</b>
<b>${today}</b>

<b>💼 Portfolio Value:</b> ${formatCurrency(metrics.total_value)}

<b>${dayChangeEmoji} Day's Change:</b> ${dayChangeSign}${formatCurrency(metrics.total_day_change)} (${dayChangeSign}${metrics.day_change_percent.toFixed(2)}%)

<b>🚀 Biggest Gainer:</b>
${metrics.biggest_gainer.name}: <b>+${metrics.biggest_gainer.percent.toFixed(2)}%</b>

<b>📉 Biggest Loser:</b>
${metrics.biggest_loser.name}: <b>${metrics.biggest_loser.percent.toFixed(2)}%</b>

<i>Total Stocks: ${metrics.total_stocks}</i>
`.trim();
}

serve(async (req) => {
  try {
    console.log('='.repeat(60));
    console.log('🇺🇸 US Portfolio Telegram Alert - Edge Function');
    console.log('='.repeat(60));
    
    // Fetch portfolio data
    console.log('\n📊 Fetching US portfolio data from Google Sheets...');
    const portfolioData = await fetchGoogleSheetsData();
    
    if (!portfolioData.length) {
      return new Response(
        JSON.stringify({ error: 'No portfolio data fetched' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      );
    }
    
    // Calculate metrics
    console.log('\n📈 Calculating metrics...');
    const metrics = calculateMetrics(portfolioData);
    
    if (!metrics) {
      return new Response(
        JSON.stringify({ error: 'Failed to calculate metrics' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      );
    }
    
    console.log(`   Portfolio Value: ${formatCurrency(metrics.total_value)}`);
    console.log(`   Day Change: ${formatCurrency(metrics.total_day_change)} (${metrics.day_change_percent > 0 ? '+' : ''}${metrics.day_change_percent.toFixed(2)}%)`);
    console.log(`   Biggest Gainer: ${metrics.biggest_gainer.name} (+${metrics.biggest_gainer.percent.toFixed(2)}%)`);
    console.log(`   Biggest Loser: ${metrics.biggest_loser.name} (${metrics.biggest_loser.percent.toFixed(2)}%)`);
    
    // Send Telegram alert
    console.log('\n📱 Sending Telegram alert...');
    const message = formatTelegramMessage(metrics);
    const success = await sendTelegramMessage(message);
    
    if (success) {
      console.log('\n✅ US Portfolio alert sent successfully!');
      return new Response(
        JSON.stringify({ success: true, message: 'Alert sent successfully', metrics }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      );
    } else {
      return new Response(
        JSON.stringify({ error: 'Failed to send Telegram message' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      );
    }
  } catch (e) {
    console.error('Error in edge function:', e);
    return new Response(
      JSON.stringify({ error: String(e) }),
      { status: 500, headers: { 'Content-Type': 'application/json' } }
    );
  }
});
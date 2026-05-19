// Setup type definitions for built-in Supabase Runtime APIs
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

console.info('server started');

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

// ============ CONFIGURATION ============
const GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQpKobYC_r4HJcUca6Q5XIXLYobdLLzksOxaScAMGMAqRE41K-yu-LRjz7azM-lM2tL2OKV82E9_Omj/pub?output=csv";

function parseNumber(valueStr) {
  if (!valueStr) return 0;
  try {
    return parseFloat(valueStr.replace(/,/g, ''));
  } catch  {
    return 0;
  }
}

serve(async (req)=>{
  try {
    console.log('='.repeat(60));
    console.log('📸 US Portfolio Daily Snapshot - Edge Function');
    console.log('='.repeat(60));
    
    // Initialize Supabase client
    const supabaseClient = createClient(
      Deno.env.get('SUPABASE_URL') ?? '', 
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') ?? ''
    );
    
    // Step 1: Get current date in EST
    const estDate = new Date().toLocaleDateString('en-US', {
      timeZone: 'America/New_York',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit'
    });
    
    const [month, day, year] = estDate.split('/');
    const snapshotDate = `${year}-${month}-${day}`;
    console.log(`📅 Snapshot date (EST): ${snapshotDate}`);
    
    // Step 2: Check if snapshot already exists
    const { data: existingSnapshot } = await supabaseClient
      .from('portfolio_snapshots')
      .select('id')
      .eq('portfolio', 'US')
      .eq('snapshot_date', snapshotDate)
      .maybeSingle();
    
    if (existingSnapshot) {
      console.log('⚠️ Snapshot already exists for today');
      return new Response(JSON.stringify({
        success: true,
        message: 'Snapshot already exists',
        date: snapshotDate
      }), {
        status: 200,
        headers: {
          'Content-Type': 'application/json'
        }
      });
    }
    
    // Step 3: Fetch live prices from Google Sheets
    console.log('📊 Fetching live prices from Google Sheets...');
    const pricesResponse = await fetch(GOOGLE_SHEET_URL);
    const csvText = await pricesResponse.text();
    const lines = csvText.trim().split('\n');
    const pricesMap = {};
    
    for (const line of lines){
      const row = line.split(',').map((cell)=>cell.trim().replace(/^"|"$/g, ''));
      if (row.length < 2 || !row[0] || !row[1]) continue;
      
      const symbol = row[0].trim();
      let ticker = symbol;
      if (symbol.includes(':')) {
        ticker = symbol.split(':')[1].trim();
      }
      
      try {
        const price = parseNumber(row[1]);
        if (price > 0) {
          pricesMap[ticker] = price;
        }
      } catch (e) {
        console.error(`Error parsing ${symbol}:`, e);
      }
    }
    
    console.log(`✅ Fetched prices for ${Object.keys(pricesMap).length} tickers`);
    
    // Step 4: Get current holdings from Supabase
    console.log('💼 Fetching holdings from Supabase...');
    const { data: holdings, error: holdingsError } = await supabaseClient
      .from('current_portfolio')
      .select('ticker, quantity, avg_cost')
      .eq('portfolio', 'US');
    
    if (holdingsError) throw holdingsError;
    console.log(`✅ Fetched ${holdings?.length || 0} holdings`);
    
    // Step 5: Calculate total portfolio value
    let totalValue = 0;
    if (holdings) {
      for (const holding of holdings){
        const price = pricesMap[holding.ticker] || holding.avg_cost;
        const value = holding.quantity * price;
        totalValue += value;
        console.log(`   ${holding.ticker}: ${holding.quantity} × $${price.toFixed(2)} = $${value.toFixed(2)}`);
      }
    }
    
    console.log(`\n💰 Total Portfolio Value: $${totalValue.toFixed(2)}`);
    
    // ============ BENCHMARK CALCULATION (NEW) ============
    // Calculate QQQ benchmark XIRR
    const qqqPrice = pricesMap['QQQ'] || 0;
    const qqqStartPrice = 268.65;
    const qqqStartDate = new Date('2023-01-02');
    const today = new Date();
    const yearsDiff = (today - qqqStartDate) / (1000 * 60 * 60 * 24 * 365.25);
    const qqqXIRR = qqqPrice > 0 
      ? ((Math.pow(qqqPrice / qqqStartPrice, 1/yearsDiff) - 1) * 100) 
      : 0;
    
    console.log(`📊 QQQ Benchmark: $${qqqPrice.toFixed(2)} | XIRR: ${qqqXIRR.toFixed(2)}%`);
    // ============ END BENCHMARK CALCULATION ============
    
    // Step 6: Save snapshot to database
    console.log('💾 Saving snapshot to database...');
    const { error: insertError } = await supabaseClient
      .from('portfolio_snapshots')
      .insert({
        portfolio: 'US',
        snapshot_date: snapshotDate,
        total_value: totalValue,
        benchmark_ticker: 'QQQ',
        benchmark_price: qqqPrice,
        benchmark_xirr: qqqXIRR
      });
    
    if (insertError) throw insertError;
    
    console.log('✅ Snapshot saved successfully!');
    console.log('='.repeat(60));
    
    return new Response(JSON.stringify({
      success: true,
      message: 'Snapshot created successfully',
      date: snapshotDate,
      value: totalValue,
      holdings_count: holdings?.length || 0,
      benchmark_xirr: qqqXIRR
    }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  } catch (error) {
    console.error('❌ Error:', error);
    return new Response(JSON.stringify({
      success: false,
      error: String(error)
    }), {
      status: 500,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  }
});

Deno.serve(async (req)=>{
  const { name } = await req.json();
  const data = {
    message: `Hello ${name}!`
  };
  
  return new Response(JSON.stringify(data), {
    headers: {
      'Content-Type': 'application/json',
      'Connection': 'keep-alive'
    }
  });
});
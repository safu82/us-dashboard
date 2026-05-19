from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import json
import csv

GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQpKobYC_r4HJcUca6Q5XIXLYobdLLzksOxaScAMGMAqRE41K-yu-LRjz7azM-lM2tL2OKV82E9_Omj/pub?output=csv"
MTM_SHEET_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRbiNUqvZC7Mdnz6d9s7WllD_so6zl_KUQvurRQc0lh8xMF1yNbi-FmFYH4kcBrJXxfPqwcE8ndNhDL/pub?output=csv"

class ProxyHandler(BaseHTTPRequestHandler):
    def _set_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
    
    def do_OPTIONS(self):
        self.send_response(200)
        self._set_headers()
        self.end_headers()
    
    def do_GET(self):
        parsed_path = urlparse(self.path)
        
        # MTM prices endpoint
        if parsed_path.path == '/mtm-prices':
            try:
                with urllib.request.urlopen(MTM_SHEET_URL) as response:
                    sheet_data = response.read().decode('utf-8')
                
                mtm_prices = {}
                reader = csv.reader(sheet_data.splitlines())
                next(reader)  # Skip header
                
                for row in reader:
                    if len(row) >= 2 and row[0] and row[1]:
                        symbol = row[0].strip()
                        
                        # Extract ticker from symbol (remove exchange prefix if any)
                        if ':' in symbol:
                            ticker = symbol.split(':')[1].strip()
                        else:
                            ticker = symbol.strip()
                        
                        try:
                            price = float(row[1].replace(',', ''))
                            mtm_prices[ticker] = price
                            print(f"  {symbol} → {ticker} = {price}")
                        except ValueError:
                            continue
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self._set_headers()
                self.end_headers()
                self.wfile.write(json.dumps(mtm_prices).encode())
                
                print(f'✅ Fetched {len(mtm_prices)} MTM prices')
                
            except Exception as e:
                self.send_response(500)
                self._set_headers()
                self.end_headers()
                error = {'error': str(e)}
                self.wfile.write(json.dumps(error).encode())
                print(f'❌ MTM Error: {e}')
            return
        
        # Regular price endpoint
        try:
            with urllib.request.urlopen(GOOGLE_SHEET_URL) as response:
                sheet_data = response.read().decode('utf-8')
            
            prices_map = {}
            reader = csv.reader(sheet_data.splitlines())
            
            for row in reader:
                if len(row) >= 5 and row[0] and row[1]:
                    symbol = row[0].strip()
                    
                    # Extract ticker from symbol (remove exchange prefix if any)
                    if ':' in symbol:
                        ticker = symbol.split(':')[1].strip()
                    else:
                        ticker = symbol.strip()
                    
                    try:
                        price = float(row[1].replace(',', '')) if row[1] else 0
                        change = float(row[2].replace(',', '')) if row[2] else 0
                        day_change_amount = float(row[4].replace(',', '')) if len(row) > 4 and row[4] else 0
                        
                        print(f"{ticker}: price={price}, change={change}, day_change={day_change_amount}")
                        
                        # Calculate change percentage from absolute change
                        prev_close = price - change
                        change_pct = (change / prev_close * 100) if prev_close > 0 else 0
                        
                        prices_map[ticker] = {
                            'price': price, 
                            'change_pct': change_pct,
                            'day_change_amount': day_change_amount
                        }
                    except ValueError as e:
                        print(f"Error parsing {symbol}: {e}")
                        continue
            
            # Convert to Yahoo Finance format
            yahoo_format = {
                "quoteResponse": {
                    "results": []
                }
            }
            
            for symbol, data in prices_map.items():
                yahoo_format["quoteResponse"]["results"].append({
                    "symbol": symbol,
                    "regularMarketPrice": data['price'],
                    "previousClose": data['price'] - (data['day_change_amount'] / 1),  # Approximate
                    "regularMarketChangePercent": data['change_pct'],
                    "dayChangeAmount": data['day_change_amount']
                })
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._set_headers()
            self.end_headers()
            self.wfile.write(json.dumps(yahoo_format).encode())
            
            print(f'✅ Fetched {len(prices_map)} US stock prices with day change amounts')
            
        except Exception as e:
            self.send_response(500)
            self._set_headers()
            self.end_headers()
            error = {'error': str(e)}
            self.wfile.write(json.dumps(error).encode())
            print(f'❌ Error: {e}')

if __name__ == '__main__':
    server = HTTPServer(('localhost', 3001), ProxyHandler)
    print('✅ US Portfolio Google Sheets proxy running on port 3001')
    print('📊 Fetching from: ' + GOOGLE_SHEET_URL)
    server.serve_forever()
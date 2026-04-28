import requests
import sqlite3
import json
import time
from datetime import datetime

def fetch_nse_data():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.nseindia.com/market-data/debt-market-reporting-corporate-bonds-traded-on-exchange',
        'X-Requested-With': 'XMLHttpRequest'
    }
    
    session = requests.Session()
    session.headers.update(headers)
    
    print("Fetching main page to get cookies...")
    try:
        # Get cookies
        session.get('https://www.nseindia.com', timeout=10)
        time.sleep(2)
        
        print("Fetching API data...")
        # The API endpoint for corporate bonds traded on exchange
        api_url = 'https://www.nseindia.com/api/liveCorp-bonds?index=otctrades_listed&marketType=CBM'
        
        response = session.get(api_url, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        return data
    except Exception as e:
        print(f"Error fetching data: {e}")
        return None

def save_to_db(data, db_path='nse_bonds.db'):
    if not data or 'data' not in data:
        print("No valid data to save.")
        return
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS corporate_bonds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        isin TEXT,
        descriptor TEXT,
        wap REAL,
        way REAL,
        no_of_trades INTEGER,
        trade_value REAL,
        ltp REAL,
        lty REAL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Insert data
    records = data.get('data', [])
    print(f"Found {len(records)} records to insert.")
    
    for item in records:
        try:
            cursor.execute('''
            INSERT INTO corporate_bonds (
                isin, descriptor, wap, way, no_of_trades, trade_value, ltp, lty
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item.get('isin', ''),
                item.get('descriptor', '').strip(),
                item.get('wap', 0.0),
                item.get('way', 0.0),
                item.get('noOfTrades', 0),
                item.get('tradeValue', 0.0),
                item.get('ltp', 0.0),
                item.get('lty', 0.0)
            ))
        except Exception as e:
            print(f"Error inserting record {item.get('isin')}: {e}")
            
    conn.commit()
    conn.close()
    print(f"Successfully saved {len(records)} records to {db_path}")

if __name__ == '__main__':
    data = fetch_nse_data()
    if data:
        save_to_db(data, 'd:\\investment_j\\Private3\\nse_bonds.db')

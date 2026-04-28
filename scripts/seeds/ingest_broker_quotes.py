import pandas as pd
import sqlite3
import glob
import os
import re
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = os.getenv("BROKER_QUOTES_DB_PATH", str(BASE_DIR / "backend" / "data" / "bond_gateway.db"))

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def clean_column_name(col):
    if not isinstance(col, str):
        return str(col)
    return col.strip().lower().replace('\n', ' ').replace('.', '').replace('  ', ' ')

def find_header_row(df, keywords=['isin', 'security', 'scrip']):
    """
    Scans first 20 rows to find a row that contains at least one of the keywords.
    Returns the index and the row itself.
    """
    for i in range(min(20, len(df))):
        row_values = [str(x).lower() for x in df.iloc[i].tolist()]
        if any(k in " ".join(row_values) for k in keywords):
            return i
    return -1

def parse_quotes(file_path):
    print(f"Processing {os.path.basename(file_path)}...")
    
    try:
        # Read first few rows to find header
        df_raw = pd.read_excel(file_path, header=None)
        header_idx = find_header_row(df_raw)
        
        if header_idx == -1:
            print(f"Could not find header for {file_path}")
            return []
            
        # Reload with correct header
        df = pd.read_excel(file_path, header=header_idx)
        
        # Clean columns
        df.columns = [clean_column_name(c) for c in df.columns]
        
        quotes = []
        
        # Identify columns dynamically
        col_map = {
            'isin': next((c for c in df.columns if 'isin' in c), None),
            'price': next((c for c in df.columns if 'price' in c and 'clean' not in c), None), # prioritization
            'yield': next((c for c in df.columns if 'ytm' in c or 'yield' in c), None),
            'quantum': next((c for c in df.columns if 'quantum' in c or 'available' in c or 'volume' in c), None),
            'bid': next((c for c in df.columns if 'bid' in c), None),
            'ask': next((c for c in df.columns if 'ask' in c or 'offer' in c), None) # often just 'Price' is Ask
        }

        # If price is ambiguous (like 'price per 100'), treat as Ask if no explicit Bid/Ask
        if not col_map['ask'] and col_map['price']:
            col_map['ask'] = col_map['price']
            
        if not col_map['isin']:
             print(f"Skipping {file_path}: No ISIN column found in {df.columns}")
             return []

        broker_name = "Unknown"
        fname = os.path.basename(file_path).lower()
        if "darashaw" in fname: broker_name = "Darashaw"
        elif "nuvama" in fname: broker_name = "Nuvama"
        elif "rfs" in fname: broker_name = "RFS"
        elif "bk" in fname: broker_name = "B&K"
        else: broker_name = "Generic"
        
        file_date = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%d')
        # Try to extract date from filename
        # e.g. 16th Feb 2026, 2026-02-19
        date_match = re.search(r'(\d{1,2})[\w\s-]*([A-Za-z]{3})[\w\s-]*(\d{4})', fname)
        if date_match:
             try:
                 d_str = f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}"
                 file_date = datetime.strptime(d_str, "%d %b %Y").strftime('%Y-%m-%d')
             except: pass
        
        # Fallback for ISO dates
        iso_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', fname)
        if iso_match:
            file_date = f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"

        for _, row in df.iterrows():
            isin = str(row[col_map['isin']]).strip()
            
            # Skip invalid ISINs
            if not isin.startswith("IN"):
                continue
                
            ask = None
            if col_map['ask']:
                try: ask = float(str(row[col_map['ask']]).replace(',',''))
                except: pass
            
            yld = None
            if col_map['yield']:
                try: 
                    val = str(row[col_map['yield']]).replace('%','')
                    yld = float(val)
                    if yld < 1: yld *= 100 # Normalize 0.07 to 7.0
                except: pass
                
            qty = None
            if col_map['quantum']:
                # Parse "5 Cr", "100 Lakhs" etc.
                q_str = str(row[col_map['quantum']]).lower()
                mult = 1
                if 'cr' in q_str: mult = 10000000
                elif 'lakh' in q_str or 'lac' in q_str: mult = 100000
                
                try:
                    num = re.findall(r"[-+]?\d*\.\d+|\d+", q_str)
                    if num:
                        qty = float(num[0]) * mult
                except: pass

            quotes.append((
                isin, broker_name, file_date,
                None, ask, # bid, ask
                None, yld, # bid_yld, ask_yld
                qty, os.path.basename(file_path)
            ))
            
        print(f"Extracted {len(quotes)} quotes from {broker_name}")
        return quotes

    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return []

def main():
    conn = get_db_connection()
    c = conn.cursor()
    
    # Clear old quotes? Or append? Let's just append but maybe clear if re-running same file?
    # For now, simplistic ingestion.
    
    files_glob = os.getenv("BROKER_QUOTES_GLOB", str(BASE_DIR / "data" / "*.xlsx"))
    files = glob.glob(files_glob)
    total_inserted = 0
    
    for f in files:
        if "investmentregister" in f.lower(): continue
        
        quotes = parse_quotes(f)
        if quotes:
            c.executemany("""
                INSERT INTO unified_broker_quotes 
                (isin, broker_name, quote_date, bid_price, ask_price, bid_yield, ask_yield, lot_size, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, quotes)
            total_inserted += len(quotes)
            
            # Also update unified_market_data with latest price found
            market_data = []
            for q in quotes:
                # q = (isin, broker_name, quote_date, bid, ask, bid_yield, ask_yield, qty, source)
                # We need: isin, price_date, clean_price, ytm, data_source
                isin, broker, date, _, ask, _, yld, _, source = q
                if ask or yld:
                    market_data.append((
                        isin, date, ask, yld, f"BROKER:{broker}", ask, yld
                    ))
            
            c.executemany("""
                INSERT OR REPLACE INTO unified_market_data
                (isin, price_date, clean_price, ytm, data_source, ask_yield, bid_yield, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, [(m[0], m[1], m[2], m[3], m[4], m[6], m[5]) for m in market_data])
            # Note: broker quotes usually give Ask/Offer price/yield. So we map to clean_price/ytm for valuation.

            
    conn.commit()
    conn.close()
    print(f"Total inserted: {total_inserted}")

if __name__ == "__main__":
    main()

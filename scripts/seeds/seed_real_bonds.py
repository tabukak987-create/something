import sqlite3
import os
from datetime import datetime
from pathlib import Path

def seed_real_bonds():
    base_dir = Path(__file__).resolve().parents[2]
    # Connect to the NSE bonds DB
    nse_db_path = os.getenv("NSE_SOURCE_DB_PATH", str(base_dir / "data" / "nse_bonds.db"))
    if not os.path.exists(nse_db_path):
        print(f"NSE DB not found at {nse_db_path}")
        return
        
    nse_conn = sqlite3.connect(nse_db_path)
    nse_conn.row_factory = sqlite3.Row
    nse_cursor = nse_conn.cursor()
    
    # Connect to the gateway DB
    gw_db_path = os.getenv("GATEWAY_DB_PATH", str(base_dir / "backend" / "data" / "bond_gateway.db"))
    gw_conn = sqlite3.connect(gw_db_path)
    gw_cursor = gw_conn.cursor()
    
    # Fetch all bonds from NSE DB
    nse_cursor.execute("SELECT * FROM corporate_bonds")
    nse_bonds = nse_cursor.fetchall()
    
    print(f"Found {len(nse_bonds)} bonds in NSE DB")
    
    inserted = 0
    for bond in nse_bonds:
        isin = bond['isin']
        if not isin:
            continue
            
        # Check if bond already exists in gateway DB
        gw_cursor.execute("SELECT COUNT(*) FROM universal_bond_master WHERE isin = ?", (isin,))
        if gw_cursor.fetchone()[0] > 0:
            continue
            
        # Insert into gateway DB
        try:
            gw_cursor.execute('''
                INSERT INTO universal_bond_master (
                    isin, bond_name, issuer_name, issuer_type, coupon_rate, 
                    coupon_type, coupon_frequency, maturity_date, issue_date, 
                    face_value, credit_rating, rating_agency, sector, 
                    sub_sector, listing_exchange, is_pf_eligible, is_active, 
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                isin,
                bond['descriptor'],
                bond['descriptor'].split(' ')[0] + ' ' + bond['descriptor'].split(' ')[1] if len(bond['descriptor'].split(' ')) > 1 else bond['descriptor'],
                'CORPORATE',
                7.5, # Default
                'FIXED',
                2,
                '2030-01-01', # Default
                '2020-01-01', # Default
                100000.0,
                'AAA',
                'CRISIL',
                'FINANCE',
                'NBFC',
                'NSE',
                1,
                1,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))
            inserted += 1
        except Exception as e:
            print(f"Error inserting {isin}: {e}")
            
    gw_conn.commit()
    
    # Also insert into market_data
    market_inserted = 0
    for bond in nse_bonds:
        isin = bond['isin']
        if not isin:
            continue
            
        try:
            gw_cursor.execute('''
                INSERT INTO market_data (
                    isin, trade_date, clean_price, ytm, volume, 
                    bid_ask_spread, modified_duration, gsec_spread, 
                    liquidity_score, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                isin,
                datetime.now().strftime('%Y-%m-%d'),
                bond['ltp'],
                bond['lty'],
                bond['no_of_trades'],
                0.1,
                4.0,
                0.5,
                0,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))
            market_inserted += 1
        except Exception as e:
            print(f"Error inserting market data for {isin}: {e}")
            
    gw_conn.commit()
    
    nse_conn.close()
    gw_conn.close()
    
    print(f"Successfully inserted {inserted} bonds into universal_bond_master")
    print(f"Successfully inserted {market_inserted} records into market_data")

if __name__ == '__main__':
    seed_real_bonds()
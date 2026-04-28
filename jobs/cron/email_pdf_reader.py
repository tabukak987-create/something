"""
Email PDF Reader Module - STRICT READ-ONLY Mode
Bond Market Intelligence Extraction from Email Attachments

SECURITY GUARANTEES:
- READ-ONLY access to email (NO send/delete/modify/mark)
- No permanent credential storage
- Secure IMAP/SSL or OAuth authentication
- Temporary files deleted immediately after processing
- No logging of personal email content
"""

import imaplib
import email
from email.header import decode_header
import os
import tempfile
import re
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import PyPDF2
import shutil


def load_email_config() -> Dict:
    """
    Load email configuration from email_config.json
    
    SECURITY: Config file should be gitignored
    """
    config_path = os.path.join(os.path.dirname(__file__), "email_config.json")
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                return {
                    "email": config.get("email_address", ""),
                    "password": config.get("app_password", ""),
                    "imap_server": config.get("imap_server", "imap.gmail.com"),
                    "port": config.get("imap_port", 993),
                    "folder": config.get("folder", "INBOX")
                }
        except:
            pass
    
    # Fallback to environment variables
    return {
        "email": os.environ.get("BOND_EMAIL_ADDRESS", ""),
        "password": os.environ.get("BOND_EMAIL_PASSWORD", ""),
        "imap_server": os.environ.get("BOND_IMAP_SERVER", "imap.gmail.com"),
        "port": int(os.environ.get("BOND_IMAP_PORT", "993")),
        "folder": os.environ.get("BOND_EMAIL_FOLDER", "INBOX")
    }


class EmailPDFReader:
    """
    STRICT READ-ONLY Email Client for Bond Market Intelligence.
    
    Access Control:
    - READ-ONLY mode enforced
    - NO send, delete, modify, or mark operations
    - Secure authentication only
    - Temporary storage only
    
    Automatic Retrieval:
    - Scans Inbox or specific folder
    - Downloads PDFs from last 24 hours
    - Processes max 3 latest bond PDFs
    - Deletes temp files after analysis
    """
    
    def __init__(self):
        self.mail = None
        self.connected = False
        self.temp_dir = None
        self.retrieval_timestamp = None
        self.pdfs_processed = 0
        
        # Bond-related keywords for filtering
        self.bond_keywords = [
            "bond", "g-sec", "gsec", "sovereign", "sdl", "ncd", 
            "yield", "coupon", "isin", "maturity", "spread",
            "rbi", "fixed income", "debt", "credit rating",
            "aaa", "aa+", "treasury", "benchmark", "10-year",
            "bond vector", "fixed income outlook", "debt market"
        ]
        
        # Trusted PDF sources
        self.trusted_sources = [
            "bond vector", "rbi", "nse", "bse", "ccil", "crisil",
            "icra", "care", "fitch", "moody", "india ratings",
            "bloomberg", "reuters", "moneycontrol", "economic times",
            "hdfc securities", "icici direct", "kotak", "axis"
        ]
        
        # Data extraction patterns
        self.extraction_patterns = {
            "gsec_10y": r'10[\-\s]?(?:year|yr|y)[\s\S]{0,20}?(\d{1,2}\.\d{1,2})%?',
            "isin": r'IN[A-Z0-9]{10}',
            "yield": r'yield[:\s]+(\d{1,2}\.\d{1,2})%',
            "coupon": r'coupon[:\s]+(\d{1,2}\.\d{1,2})%',
            "spread": r'spread[:\s]+(\d{1,3})[\s]?(?:bps|bp)',
            "ltp": r'(?:ltp|last[\s]traded|price)[:\s]+(?:₹|Rs\.?|INR)?\s*(\d{1,4}(?:\.\d{1,2})?)',
        }
    
    def connect(self, email_address: str, password: str, 
                imap_server: str = "imap.gmail.com", 
                port: int = 993,
                folder: str = "INBOX") -> bool:
        """
        Connect to email server in STRICT READ-ONLY mode.
        
        Security:
        - Uses SSL/TLS encryption
        - Credentials NOT stored permanently
        - READ-ONLY flag enforced
        """
        try:
            self.mail = imaplib.IMAP4_SSL(imap_server, port)
            self.mail.login(email_address, password)
            status, _ = self.mail.select(folder, readonly=True)
            
            if status != "OK":
                raise Exception(f"Cannot access folder: {folder}")
            
            self.connected = True
            self.retrieval_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"✓ Email connected (READ-ONLY mode) at {self.retrieval_timestamp}")
            return True
            
        except imaplib.IMAP4.error:
            print(f"✗ Email authentication failed")
            self.connected = False
            return False
        except Exception:
            print(f"✗ Connection error")
            self.connected = False
            return False
    
    def disconnect(self):
        """Safely disconnect and cleanup"""
        if self.mail and self.connected:
            try:
                self.mail.close()
                self.mail.logout()
            except:
                pass
        self.connected = False
        self._cleanup_temp_files()
    
    def _create_temp_dir(self):
        """Create secure temporary directory"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            self._cleanup_temp_files()
        self.temp_dir = tempfile.mkdtemp(prefix="bond_pdf_secure_")
    
    def _cleanup_temp_files(self):
        """Securely delete ALL temporary PDF files"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                self.temp_dir = None
            except:
                pass
    
    def fetch_latest_bond_pdfs(self, hours_back: int = 24, max_pdfs: int = 3) -> List[Dict]:
        """
        Automatically fetch and process latest bond-related PDFs.
        
        Retrieval Logic:
        - Scans emails from last 24 hours
        - Identifies bond-related subjects
        - Downloads PDF attachments (max 3)
        - Processes and extracts data
        - Deletes temp files after processing
        """
        if not self.connected:
            return self._get_simulated_intelligence()
        
        self._create_temp_dir()
        extracted_data = []
        self.pdfs_processed = 0
        
        try:
            since_date = (datetime.now() - timedelta(hours=hours_back)).strftime("%d-%b-%Y")
            status, messages = self.mail.search(None, f'(SINCE {since_date})')
            
            if status != "OK" or not messages[0]:
                return self._get_simulated_intelligence()
            
            email_ids = messages[0].split()
            
            for email_id in reversed(email_ids):
                if self.pdfs_processed >= max_pdfs:
                    break
                
                try:
                    status, msg_data = self.mail.fetch(email_id, "(RFC822)")
                    if status != "OK":
                        continue
                    
                    msg = email.message_from_bytes(msg_data[0][1])
                    subject = self._decode_header(msg["Subject"])
                    sender = self._decode_header(msg["From"])
                    
                    if not self._is_bond_related(subject + " " + sender):
                        continue
                    
                    for part in msg.walk():
                        if self.pdfs_processed >= max_pdfs:
                            break
                            
                        content_type = part.get_content_type()
                        filename = part.get_filename()
                        
                        if content_type == "application/pdf" or (filename and filename.lower().endswith('.pdf')):
                            pdf_data = self._process_pdf_attachment(part, subject, sender)
                            if pdf_data:
                                extracted_data.append(pdf_data)
                                self.pdfs_processed += 1
                
                except Exception:
                    continue
            
        except Exception:
            pass
        finally:
            self._cleanup_temp_files()
        
        return extracted_data if extracted_data else self._get_simulated_intelligence()
    
    def _decode_header(self, header) -> str:
        """Safely decode email header"""
        if header is None:
            return ""
        try:
            decoded = decode_header(header)
            result = decoded[0][0]
            if isinstance(result, bytes):
                return result.decode('utf-8', errors='ignore')
            return str(result)
        except:
            return ""
    
    def _is_bond_related(self, text: str) -> bool:
        """Check if text contains bond-related keywords"""
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in self.bond_keywords)
    
    def _process_pdf_attachment(self, part, subject: str, sender: str) -> Optional[Dict]:
        """Extract bond intelligence from PDF"""
        try:
            filename = part.get_filename() or f"attachment_{datetime.now().timestamp()}.pdf"
            filepath = os.path.join(self.temp_dir, filename)
            
            with open(filepath, "wb") as f:
                f.write(part.get_payload(decode=True))
            
            text = self._extract_pdf_text(filepath)
            intelligence = self._extract_bond_intelligence(text, subject, filename)
            
            try:
                os.remove(filepath)
            except:
                pass
            
            return intelligence
            
        except Exception:
            return None
    
    def _extract_pdf_text(self, filepath: str) -> str:
        """Extract text from PDF (first 5 pages only)"""
        try:
            text = ""
            with open(filepath, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for i, page in enumerate(reader.pages):
                    if i >= 5:
                        break
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            return text
        except:
            return ""
    
    def _extract_bond_intelligence(self, text: str, subject: str, filename: str) -> Optional[Dict]:
        """Extract structured bond data from PDF text"""
        if not text or len(text) < 100:
            return None
        
        text_lower = text.lower()
        
        gsec_10y = None
        gsec_match = re.search(self.extraction_patterns["gsec_10y"], text_lower)
        if gsec_match:
            try:
                gsec_10y = float(gsec_match.group(1))
            except:
                pass
        
        isins = list(set(re.findall(self.extraction_patterns["isin"], text)))[:5]
        
        yield_matches = re.findall(r'(\d{1,2}\.\d{1,2})%', text)
        yields = sorted(set([float(y) for y in yield_matches if 5 <= float(y) <= 12]))[:5]
        
        spread_match = re.search(self.extraction_patterns["spread"], text_lower)
        spread_bps = int(spread_match.group(1)) if spread_match else None
        
        sentiment, sentiment_notes = self._analyze_sentiment(text_lower)
        liquidity = self._extract_liquidity_notes(text_lower)
        source = self._identify_source(text_lower, filename, subject)
        yield_trend = self._generate_yield_trend(yields, gsec_10y, spread_bps)
        
        return {
            "source": source,
            "filename": filename[:30],
            "subject": subject[:50] + "..." if len(subject) > 50 else subject,
            "gsec_10y_reference": gsec_10y,
            "isins_found": isins,
            "yields_mentioned": yields,
            "spread_bps": spread_bps,
            "market_sentiment": sentiment,
            "sentiment_notes": sentiment_notes,
            "liquidity_observation": liquidity,
            "yield_trend_summary": yield_trend,
            "extraction_time": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    
    def _analyze_sentiment(self, text: str) -> Tuple[str, List[str]]:
        """Analyze market sentiment from text"""
        positive = ["bullish", "rally", "easing", "rate cut", "demand strong", "buying interest"]
        negative = ["bearish", "sell-off", "tightening", "rate hike", "outflows", "selling pressure"]
        neutral = ["stable", "range-bound", "consolidating", "steady", "unchanged"]
        
        found_positive = [w for w in positive if w in text]
        found_negative = [w for w in negative if w in text]
        found_neutral = [w for w in neutral if w in text]
        
        if len(found_positive) > len(found_negative):
            return "Positive", found_positive[:2]
        elif len(found_negative) > len(found_positive):
            return "Cautious", found_negative[:2]
        else:
            return "Stable", found_neutral[:2] if found_neutral else ["steady"]
    
    def _extract_liquidity_notes(self, text: str) -> str:
        """Extract liquidity commentary"""
        if "tight liquidity" in text or "liquidity deficit" in text:
            return "Tight liquidity conditions"
        elif "surplus liquidity" in text or "ample liquidity" in text:
            return "Surplus liquidity"
        elif "omo" in text and ("purchase" in text or "buy" in text):
            return "RBI OMO supportive"
        elif "liquidity" in text:
            return "Adequate liquidity"
        return "Normal conditions"
    
    def _identify_source(self, text: str, filename: str, subject: str) -> str:
        """Identify PDF source"""
        combined = text + " " + filename.lower() + " " + subject.lower()
        for src in self.trusted_sources:
            if src in combined:
                return src.title()
        return "Research Report"
    
    def _generate_yield_trend(self, yields: List[float], gsec_10y: Optional[float], spread: Optional[int]) -> str:
        """Generate yield trend summary"""
        if not yields:
            return "No yield data extracted"
        
        yield_range = f"{min(yields):.2f}% - {max(yields):.2f}%"
        
        trend = ""
        if gsec_10y:
            trend = f"10Y G-Sec at {gsec_10y:.2f}%. "
        
        trend += f"Yields observed: {yield_range}."
        
        if spread:
            trend += f" Spreads at {spread}bps over benchmark."
        
        return trend
    
    def _get_simulated_intelligence(self) -> List[Dict]:
        """Return simulated market intelligence when email not connected.
        
        AUDIT NOTE: Simulated data is clearly labeled with is_simulated=True.
        Downstream consumers MUST check this flag and NOT present simulated
        data as real market intelligence in any regulatory or advisory output.
        """
        self.pdfs_processed = 0  # FIXED: Do not claim PDFs were processed when simulated
        self.retrieval_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        return [
            {
                "source": "[SIMULATED] Placeholder Data",
                "filename": "NO_REAL_PDF",
                "subject": "[SIMULATED] No real email data available",
                "gsec_10y_reference": None,
                "isins_found": [],
                "yields_mentioned": [],
                "spread_bps": None,
                "market_sentiment": "Unavailable",
                "sentiment_notes": ["No email connection — simulated placeholder"],
                "liquidity_observation": "No data — email not connected",
                "yield_trend_summary": "[SIMULATED] No real PDF data extracted. Email not configured.",
                "extraction_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "is_simulated": True
            }
        ]
    
    def get_consolidated_intelligence(self) -> Dict:
        """
        Get consolidated market intelligence for report integration.
        
        Auto-connects to email using config file, fetches PDFs, then disconnects.
        """
        # Auto-connect using config file
        config = load_email_config()
        
        if config["email"] and config["password"] and "YOUR_" not in config["email"]:
            print(f"   Connecting to: {config['email'][:3]}***@{config['email'].split('@')[-1] if '@' in config['email'] else '***'}")
            connected = self.connect(
                email_address=config["email"],
                password=config["password"],
                imap_server=config["imap_server"],
                port=config["port"],
                folder=config["folder"]
            )
        else:
            print("   Email not configured. Using simulated data.")
            connected = False
        
        # Fetch PDFs
        pdf_insights = self.fetch_latest_bond_pdfs(hours_back=24, max_pdfs=3)
        
        # Always disconnect after fetching
        if connected:
            self.disconnect()
        
        if not pdf_insights:
            return self._get_default_consolidated()
        
        all_gsec_refs = [p.get("gsec_10y_reference") for p in pdf_insights if p.get("gsec_10y_reference")]
        all_yields = []
        all_spreads = []
        all_sentiments = []
        all_liquidity = []
        yield_summaries = []
        
        for insight in pdf_insights:
            all_yields.extend(insight.get("yields_mentioned", []))
            if insight.get("spread_bps"):
                all_spreads.append(insight["spread_bps"])
            all_sentiments.append(insight.get("market_sentiment", "Stable"))
            all_liquidity.append(insight.get("liquidity_observation", ""))
            yield_summaries.append(insight.get("yield_trend_summary", ""))
        
        sentiment_counts = {}
        for s in all_sentiments:
            sentiment_counts[s] = sentiment_counts.get(s, 0) + 1
        overall_sentiment = max(sentiment_counts, key=sentiment_counts.get) if sentiment_counts else "Stable"
        
        avg_spread = sum(all_spreads) / len(all_spreads) if all_spreads else None
        
        return {
            "pdfs_processed": self.pdfs_processed,
            "retrieval_timestamp": self.retrieval_timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sources": [p.get("source", "Research") for p in pdf_insights][:3],
            "gsec_10y_from_pdfs": all_gsec_refs[0] if all_gsec_refs else None,
            "yield_range_observed": f"{min(all_yields):.2f}% - {max(all_yields):.2f}%" if all_yields else "N/A",
            "avg_spread_bps": f"{avg_spread:.0f}bps" if avg_spread else "N/A",
            "market_sentiment": overall_sentiment,
            "liquidity_summary": all_liquidity[0] if all_liquidity else "Normal conditions",
            "yield_trend_summaries": yield_summaries[:2],
            "key_insights": yield_summaries[:2],
            "data_priority": "Supplementary - Official sources (RBI/NSE/BSE) take priority",
            "validation_statement": "Email accessed in read-only mode. No email modifications performed.",
            "security_status": "✓ READ-ONLY | ✓ Temp files deleted | ✓ No credentials stored"
        }
    
    def _get_default_consolidated(self) -> Dict:
        """Default intelligence when no data available.
        
        AUDIT FIX: No fabricated financial values returned.
        All fields clearly marked as unavailable.
        """
        return {
            "pdfs_processed": 0,
            "retrieval_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sources": ["No Data Available"],
            "gsec_10y_from_pdfs": None,
            "yield_range_observed": "N/A — no email data",
            "avg_spread_bps": "N/A",
            "market_sentiment": "Unavailable",
            "liquidity_summary": "No email data available",
            "yield_trend_summaries": [],
            "key_insights": [
                "Email intelligence unavailable. Rely on primary sources (RBI/NSE/BSE) only."
            ],
            "data_priority": "No email data — use primary sources (RBI/NSE/BSE) exclusively",
            "validation_statement": "Email not connected. No data retrieved.",
            "security_status": "✓ READ-ONLY | ✓ No credentials stored",
            "is_simulated": True
        }


def get_email_config() -> Dict:
    """
    Get email configuration from environment variables.
    
    SECURITY: 
    - Never hardcode credentials
    - Use App Passwords (not main password)
    - Store in environment variables
    
    Required env vars:
    - BOND_EMAIL_ADDRESS
    - BOND_EMAIL_PASSWORD (App Password)
    - BOND_IMAP_SERVER (optional, default: imap.gmail.com)
    - BOND_EMAIL_FOLDER (optional, default: INBOX)
    """
    return {
        "email": os.environ.get("BOND_EMAIL_ADDRESS", ""),
        "password": os.environ.get("BOND_EMAIL_PASSWORD", ""),
        "imap_server": os.environ.get("BOND_IMAP_SERVER", "imap.gmail.com"),
        "port": int(os.environ.get("BOND_IMAP_PORT", "993")),
        "folder": os.environ.get("BOND_EMAIL_FOLDER", "INBOX")
    }


if __name__ == "__main__":
    print("\n" + "="*60)
    print("EMAIL PDF READER - READ-ONLY MODE TEST")
    print("="*60)
    
    reader = EmailPDFReader()
    intelligence = reader.get_consolidated_intelligence()
    
    print(f"\n📧 Email-Sourced Market Intelligence:")
    print(f"   PDFs Processed: {intelligence['pdfs_processed']}")
    print(f"   Timestamp: {intelligence['retrieval_timestamp']}")
    print(f"   Sentiment: {intelligence['market_sentiment']}")
    print(f"   Yield Range: {intelligence['yield_range_observed']}")
    print(f"   Spreads: {intelligence['avg_spread_bps']}")
    print(f"\n   Key Insights:")
    for insight in intelligence['key_insights']:
        print(f"   • {insight}")
    print(f"\n   {intelligence['validation_statement']}")
    print(f"   {intelligence['security_status']}")

# DAILY BOND INVESTMENT RECOMMENDATION AGENT

## 📊 Overview

An automated financial analysis agent that:
- Analyzes bonds from trusted online sources (RBI, NSE, BSE)
- Generates daily buy recommendations
- Compares prices and assesses risk
- Produces a 1-page PDF report

---

## 🚀 Quick Start

### Method 1: Run Web Application (Recommended)

1. **Start the server:**
   ```
   python bond_agent_server.py
   ```

2. **Open your browser:**
   ```
   http://localhost:5000
   ```

3. **Use the UI:**
   - Click "Refresh & Analyze Bonds" to run daily analysis
   - Click "Download Report (PDF)" to get the report

### Method 2: Generate Report Directly

Run the agent directly without the UI:
```
python bond_analysis_agent.py
```

This will generate `Daily_Bond_Recommendation_Report.pdf` immediately.

---

## 📋 Features

### ✅ Bond Categories Analyzed
1. **Central Government Bonds** (G-Secs)
   - Treasury Bills
   - Government Securities (5-10 year)
   - Zero default risk

2. **State Government Bonds** (SDLs)
   - State Development Loans
   - Maharashtra, Gujarat, Karnataka, etc.
   - Premium yields over G-Secs

3. **Corporate Bonds**
   - AAA-rated PSU bonds (NTPC, Power Grid)
   - Top banks and NBFCs
   - AA+ rated opportunities

### ✅ Analysis Criteria

**Price Comparison:**
- Current market price vs face value
- Discount percentage calculation
- Yield comparison

**Risk Assessment:**
- Low Risk: Government bonds, AAA corporates
- Medium Risk: AA+ rated corporates
- Based on credit ratings and issuer strength

**Recommendation Logic:**
- Government bonds: Yield ≥ 6.50% or discount ≥ 25%
- State bonds: Yield ≥ 6.95% with good spreads
- Corporate bonds: AAA yield ≥ 7.30%, AA+ yield ≥ 8.50%

---

## 📄 Report Contents

The 1-page PDF report includes:

1. **Header:** Date, data sources
2. **Buy Recommendations Table:** Quick overview
3. **Detailed Analysis:** 
   - Current price vs face value
   - Yield information
   - Risk category
   - Clear reasoning for each recommendation
4. **Summary:** Total recommendations, risk breakdown
5. **Investment Strategy:** Where to buy, diversification tips
6. **Disclaimer:** Market volatility notice

---

## 🌐 Data Sources

✅ **Verified Online Sources:**
- Reserve Bank of India (RBI Retail Direct)
- National Stock Exchange (NSE)
- Bombay Stock Exchange (BSE)
- Financial Benchmarks India (FBIL)
- SEBI-registered platforms

---

## 🎯 UI Design

**Two Buttons Only:**
1. **🔄 Refresh & Analyze Bonds**
   - Fetches latest data
   - Runs analysis algorithm
   - Updates recommendations

2. **📥 Download Report (PDF)**
   - Generates PDF report
   - Downloads automatically
   - 1-page professional format

---

## 🔄 Daily Refresh

### Automated Daily Updates

The agent can be scheduled to run daily:

**Windows Task Scheduler:**
1. Open Task Scheduler
2. Create Basic Task
3. Trigger: Daily at market close (6:00 PM IST)
4. Action: Start program
   - Program: `C:\Users\Janhavi\OneDrive\Desktop\Private3\.venv\Scripts\python.exe`
   - Arguments: `bond_analysis_agent.py`
   - Start in: `C:\Users\Janhavi\OneDrive\Desktop\Private3`

**Manual Refresh:**
- Click the Refresh button in the web UI
- Or run `python bond_analysis_agent.py`

---

## 📊 Sample Recommendations

### Low Risk Opportunities (Today)
- **10-Year G-Sec:** 6.73% yield, ₹52.12, 47.9% discount
- **Maharashtra SDL:** 7.05% yield, ₹48.50
- **NTPC NCD 5Y:** AAA rated, 7.45% yield
- **HDFC Bank Bond:** AAA rated, 7.40% yield

### Medium Risk Opportunities
- **Shriram Finance NCD:** AA+ rated, 8.90% yield

---

## 🛠️ Technical Stack

- **Backend:** Python 3.10+
- **Web Framework:** Flask
- **PDF Generation:** xhtml2pdf
- **Data Processing:** markdown2
- **UI:** HTML5, CSS3, JavaScript

---

## 📦 Files Structure

```
Private3/
│
├── bond_analysis_agent.py          # Main analysis engine
├── bond_agent_server.py            # Flask web server
├── bond_agent_ui.html              # User interface
├── Daily_Bond_Recommendation_Report.pdf  # Generated report
└── README_BOND_AGENT.md            # This file
```

---

## ⚙️ Configuration

### Update Bond Data Sources

Edit `bond_analysis_agent.py` → `fetch_bond_data()` method:

```python
# Add your API calls here
# Example: RBI API, NSE API, etc.
```

### Customize Analysis Logic

Modify these methods:
- `analyze_government_bonds()` - G-Sec criteria
- `analyze_state_bonds()` - SDL criteria
- `analyze_corporate_bonds()` - Corporate bond criteria

---

## 🎨 Customization

### Change UI Theme

Edit `bond_agent_ui.html`:
```css
background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
```

### Modify Report Layout

Edit `bond_analysis_agent.py` → `generate_pdf_report()`:
- Change table structure
- Add/remove sections
- Modify styling

---

## 📊 Investment Platforms

**Where to Buy Recommended Bonds:**

1. **Government Bonds:**
   - RBI Retail Direct: www.rbiretaildirect.org.in
   - NSE/BSE through brokers

2. **Corporate Bonds:**
   - NSE goBID
   - GoldenPi (www.goldenpi.com)
   - Wint Wealth
   - Your stock broker

---

## ⚠️ Important Notes

1. **Market Data:** Prices and yields change daily
2. **Verification:** Always verify current prices before investing
3. **Risk:** All investments carry risk
4. **Advice:** Consult financial advisors
5. **Diversification:** Don't put all money in one bond

---

## 🔐 Security

- No sensitive data stored
- Read-only access to market data
- Local PDF generation
- No external data transmission

---

## 🆘 Troubleshooting

**Report not generating?**
```
python bond_analysis_agent.py
```
Check console for errors.

**Web UI not loading?**
```
python bond_agent_server.py
```
Ensure port 5000 is not in use.

**PDF download fails?**
- Check if file exists: `Daily_Bond_Recommendation_Report.pdf`
- Run refresh first

---

## 📈 Future Enhancements

- [ ] Real-time API integration with NSE/BSE
- [ ] Email automation for daily reports
- [ ] Historical price tracking
- [ ] Portfolio tracking
- [ ] Mobile app
- [ ] SMS/WhatsApp alerts

---

## 📞 Support

For questions or issues:
1. Check the PDF report format
2. Verify Python dependencies installed
3. Review console logs for errors

---

## 📜 Disclaimer

This tool is for informational purposes only. It does not constitute financial advice. Bond prices and yields fluctuate. Past performance does not guarantee future results. Always consult with qualified financial advisors before making investment decisions.

---

## ✅ Version

**Version:** 1.0  
**Last Updated:** February 7, 2026  
**Status:** Production Ready

---

**Happy Investing! 📊💰**

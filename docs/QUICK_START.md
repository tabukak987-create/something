# 🚀 QUICK START GUIDE

## Bond Investment Recommendation Agent

---

## ⚡ Launch the Agent (3 Ways)

### Method 1: Double-Click Launcher ⭐ EASIEST
```
📄 Start_Bond_Agent.bat
```
Just double-click this file! Your browser will open automatically.

### Method 2: Command Line
```powershell
python bond_agent_server.py
```
Then open: http://localhost:5000

### Method 3: Direct Report Generation
```powershell
python bond_analysis_agent.py
```
Report generated instantly as PDF!

---

## 🎯 How to Use the Web UI

### Step 1: Click "Refresh & Analyze Bonds"
- Agent fetches latest bond data
- Analyzes Government, State, and Corporate bonds
- Identifies buy opportunities
- Takes ~2 seconds

### Step 2: Click "Download Report (PDF)"
- Downloads 1-page PDF report
- Includes all recommendations
- Shows prices, yields, and reasoning
- Ready to share!

---

## 📊 What You Get

### Daily Report Includes:

✅ **Low Risk Bonds:**
- Central Government Securities
- State Government Bonds (SDLs)
- AAA-rated corporate bonds
- Yields: 6.50% - 8.00%

✅ **Medium Risk Bonds:**
- AA+ rated corporate bonds
- Higher yields: 8.50% - 9.00%

✅ **For Each Bond:**
- Current price vs face value
- Yield percentage
- Risk category
- Clear buy reasoning
- Where to purchase

---

## 🔄 Daily Refresh

**Manual:** Click the Refresh button anytime

**Automatic:** Set up Windows Task Scheduler:
1. Open Task Scheduler
2. Create task to run daily at 6 PM
3. Run: `bond_analysis_agent.py`

---

## 📥 Today's Sample Recommendations

**Generated:** February 7, 2026

| Bond | Price | Yield | Risk | Action |
|------|-------|-------|------|--------|
| 10-Year G-Sec | ₹52.12 | 6.73% | Low | BUY |
| Maharashtra SDL | ₹48.50 | 7.05% | Low | BUY |
| NTPC NCD 5Y | ₹100.67 | 7.45% | Low | BUY |
| Bajaj Finance NCD | ₹100.67 | 8.00% | Low | BUY |
| Shriram Finance | ₹99.80 | 8.90% | Medium | BUY |

**Total:** 11 recommendations today

---

## 🌐 Where to Buy

### Government Bonds:
🔗 RBI Retail Direct: www.rbiretaildirect.org.in

### Corporate Bonds:
🔗 NSE goBID  
🔗 GoldenPi: www.goldenpi.com  
🔗 Wint Wealth  
🔗 Your stock broker

---

## 🆘 Troubleshooting

**Problem:** Web UI won't load  
**Solution:** Make sure port 5000 is free, restart server

**Problem:** Report not downloading  
**Solution:** Click Refresh first, then Download

**Problem:** "Analysis failed"  
**Solution:** Check internet connection, run agent directly

---

## 📞 Need Help?

1. Check `README_BOND_AGENT.md` for full documentation
2. Review console logs for errors
3. Ensure Python environment is active

---

## ⚠️ Disclaimer

For informational purposes only. Not financial advice.  
Verify current prices before investing.  
Consult financial advisors.

---

**Version 1.0 | February 2026**

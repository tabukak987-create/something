import { DragEvent, FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { AnalysisResult, api, Bond, HistoryItem, Session } from './api';

type View = 'new' | 'result' | 'history' | 'register';
const accepted = ['.csv', '.xlsx', '.json', '.pdf', '.txt'];
const storedSession = (): Session | null => {
  try { return JSON.parse(localStorage.getItem('poi.session') ?? 'null') as Session | null; }
  catch { return null; }
};
const money = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 });
const number = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });
const dateTime = new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' });

export default function App() {
  const [session, setSession] = useState<Session | null>(storedSession);
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [authError, setAuthError] = useState('');
  const [view, setView] = useState<View>('new');
  const [files, setFiles] = useState<File[]>([]);
  const [caseName, setCaseName] = useState('');
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [bonds, setBonds] = useState<Bond[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [dragging, setDragging] = useState(false);

  const refresh = useCallback(async (active: Session) => {
    const [saved, register] = await Promise.all([api.history(active.accessToken), api.bonds(active.accessToken)]);
    setHistory(saved); setBonds(register);
  }, []);

  useEffect(() => { if (session) refresh(session).catch(() => logout()); }, [session, refresh]);

  function logout() {
    localStorage.removeItem('poi.session');
    setSession(null); setPassword(''); setResult(null); setFiles([]);
  }

  async function signIn(event: FormEvent) {
    event.preventDefault(); setAuthError('');
    try {
      const next = await api.login(username, password);
      localStorage.setItem('poi.session', JSON.stringify(next));
      setSession(next);
    } catch (reason) { setAuthError(reason instanceof Error ? reason.message : 'Unable to sign in'); }
  }

  function addFiles(incoming: File[]) {
    setError('');
    setFiles(current => {
      const next = [...current];
      for (const file of incoming) {
        if (!accepted.some(extension => file.name.toLowerCase().endsWith(extension))) {
          setError(`${file.name} is not supported. Use CSV, XLSX, JSON, PDF, or TXT.`); continue;
        }
        if (file.size > 20 * 1024 * 1024) { setError(`${file.name} exceeds the 20 MB limit.`); continue; }
        if (!next.some(existing => existing.name === file.name && existing.size === file.size)) next.push(file);
      }
      if (next.length > 10) { setError('A review can contain at most 10 files.'); return next.slice(0, 10); }
      return next;
    });
  }

  function drop(event: DragEvent) {
    event.preventDefault(); setDragging(false); addFiles(Array.from(event.dataTransfer.files));
  }

  async function run() {
    if (!session || files.length === 0) { setError('Select at least one file to analyse.'); return; }
    setBusy(true); setError('');
    try {
      const completed = await api.runAnalysis(caseName, files, session.accessToken);
      setResult(completed); setView('result'); setFiles([]); setCaseName('');
      await refresh(session);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Analysis failed'); }
    finally { setBusy(false); }
  }

  async function openHistory(id: string) {
    if (!session) return; setBusy(true); setError('');
    try { setResult(await api.analysis(id, session.accessToken)); setView('result'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not open analysis'); }
    finally { setBusy(false); }
  }

  async function downloadTemplate() {
    if (!session) return;
    try {
      const blob = await api.downloadTemplate(session.accessToken);
      const url = URL.createObjectURL(blob); const link = document.createElement('a');
      link.href = url; link.download = 'poi-analysis-template.csv'; link.click(); URL.revokeObjectURL(url);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Download failed'); }
  }

  if (!session) return <Login username={username} password={password} error={authError} onUsername={setUsername} onPassword={setPassword} onSubmit={signIn} />;

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">P</div><div><strong>POI</strong><small>Portfolio & Investment Oversight</small></div></div>
      <nav aria-label="Primary navigation">
        <button className={view === 'new' ? 'active' : ''} onClick={() => setView('new')}><Icon name="upload" />New analysis</button>
        <button className={view === 'result' ? 'active' : ''} onClick={() => result && setView('result')} disabled={!result}><Icon name="result" />Current result</button>
        <button className={view === 'history' ? 'active' : ''} onClick={() => setView('history')}><Icon name="history" />Analysis history<span className="count">{history.length}</span></button>
        <button className={view === 'register' ? 'active' : ''} onClick={() => setView('register')}><Icon name="bond" />Bond register<span className="count">{bonds.length}</span></button>
      </nav>
      <div className="sidebar-note"><span className="status-dot" />Analysis services connected</div>
      <button className="profile" onClick={logout}><span className="avatar">{session.username[0].toUpperCase()}</span><span><strong>{session.username}</strong><small>{session.role}</small></span><span aria-hidden>↗</span></button>
    </aside>
    <main className="main-content">
      <header className="topbar"><div><p className="kicker">Institutional fixed income</p><h1>{viewTitle(view)}</h1></div><div className="as-of">As of {dateTime.format(new Date())}</div></header>
      {error && <div className="alert" role="alert"><strong>Action needed</strong><span>{error}</span><button onClick={() => setError('')} aria-label="Dismiss">×</button></div>}
      {busy && <div className="processing"><span className="spinner" /><div><strong>Combining and analysing your files</strong><small>Validating sources, merging instruments, calculating values, and preparing findings…</small></div></div>}
      {view === 'new' && <UploadWorkspace files={files} caseName={caseName} dragging={dragging} busy={busy} onName={setCaseName} onAdd={addFiles} onRemove={index => setFiles(current => current.filter((_, i) => i !== index))} onDrop={drop} onDrag={setDragging} onRun={run} onTemplate={downloadTemplate} />}
      {view === 'result' && result && <ResultView result={result} onNew={() => { setView('new'); setResult(null); }} />}
      {view === 'history' && <HistoryView items={history} onOpen={openHistory} />}
      {view === 'register' && <RegisterView bonds={bonds} />}
    </main>
  </div>;
}

function Login(props: { username: string; password: string; error: string; onUsername: (v: string) => void; onPassword: (v: string) => void; onSubmit: (e: FormEvent) => void }) {
  return <main className="login"><form onSubmit={props.onSubmit}><div className="login-brand"><div className="brand-mark">P</div><span>POI INVESTMENT SYSTEM</span></div><h1>Investment oversight,<br />in one place.</h1><p>Sign in to combine source files and produce a traceable investment review.</p><label>Username<input value={props.username} onChange={e => props.onUsername(e.target.value)} autoComplete="username" required /></label><label>Password<input type="password" value={props.password} onChange={e => props.onPassword(e.target.value)} autoComplete="current-password" required /></label>{props.error && <output>{props.error}</output>}<button className="primary">Sign in securely</button><small>Local development account: admin / ChangeMe123!</small></form></main>;
}

function UploadWorkspace(props: { files: File[]; caseName: string; dragging: boolean; busy: boolean; onName: (v: string) => void; onAdd: (f: File[]) => void; onRemove: (i: number) => void; onDrop: (e: DragEvent) => void; onDrag: (v: boolean) => void; onRun: () => void; onTemplate: () => void }) {
  const totalSize = useMemo(() => props.files.reduce((sum, file) => sum + file.size, 0), [props.files]);
  return <div className="workspace-grid"><section className="content-card upload-card"><div className="section-heading"><div><p className="step">STEP 1 OF 2</p><h2>Bring your investment sources together</h2><p>Upload holdings, prices, broker data, or supporting documents. POI will identify instruments and merge information by ISIN.</p></div><button className="text-button" onClick={props.onTemplate}>Download CSV template</button></div><label className="case-name">Review name <input placeholder="e.g. August portfolio review" value={props.caseName} onChange={e => props.onName(e.target.value)} /></label><label className={`dropzone ${props.dragging ? 'dragging' : ''}`} onDrop={props.onDrop} onDragOver={e => { e.preventDefault(); props.onDrag(true); }} onDragLeave={() => props.onDrag(false)}><input type="file" multiple accept={accepted.join(',')} onChange={e => props.onAdd(Array.from(e.target.files ?? []))} /><span className="upload-icon">↑</span><strong>Drop files here or choose from your computer</strong><p>CSV, XLSX, JSON, PDF, or TXT · up to 10 files · 20 MB each</p><span className="choose">Choose files</span></label>{props.files.length > 0 && <div className="file-list"><div className="file-list-head"><strong>{props.files.length} source{props.files.length === 1 ? '' : 's'} ready</strong><span>{formatBytes(totalSize)}</span></div>{props.files.map((file, index) => <div className="file-row" key={`${file.name}-${file.size}`}><span className={`file-type ${extension(file.name)}`}>{extension(file.name).toUpperCase()}</span><span><strong>{file.name}</strong><small>{formatBytes(file.size)} · checksum recorded on upload</small></span><button onClick={() => props.onRemove(index)} aria-label={`Remove ${file.name}`}>×</button></div>)}</div>}<div className="run-bar"><div><strong>Ready to produce a consolidated review?</strong><small>The result is saved to your analysis history.</small></div><button className="primary" disabled={props.files.length === 0 || props.busy} onClick={props.onRun}>{props.busy ? 'Analysing…' : 'Combine & analyse'}</button></div></section><aside className="content-card process-card"><p className="step">WHAT POI WILL DO</p>{[['1','Validate','Check file type, size, and readable content'],['2','Extract','Read spreadsheet rows and document evidence'],['3','Combine','Match instruments by ISIN and retain sources'],['4','Analyse','Calculate values, completeness, and exposures'],['5','Flag','Surface missing, conflicting, and risky information'],['6','Save','Preserve files, checksums, and the final result']].map(([n,t,d]) => <div className="process-step" key={n}><span>{n}</span><div><strong>{t}</strong><p>{d}</p></div></div>)}<div className="human-review"><strong>Human review required</strong><p>Outputs are decision support, not automatic investment approval.</p></div></aside></div>;
}

function ResultView({ result, onNew }: { result: AnalysisResult; onNew: () => void }) {
  return <div className="result-stack"><section className="result-hero"><div><div className="result-meta"><Status value={result.summary.decision} /> <span>Completed {dateTime.format(new Date(result.completedAt))}</span></div><h2>{result.name}</h2><p>{result.summary.fileCount} source files combined into {result.summary.instrumentCount} instruments. All results require analyst verification.</p></div><button className="secondary" onClick={onNew}>Start another review</button></section><section className="metrics"><Metric label="Combined market value" value={money.format(result.summary.totalMarketValue)} note="From uploaded price × quantity" /><Metric label="Weighted coupon" value={`${number.format(result.summary.weightedCoupon)}%`} note="Market-value weighted" /><Metric label="Data completeness" value={`${result.summary.dataQualityScore}%`} note="Required core fields" tone={result.summary.dataQualityScore < 70 ? 'warn' : 'good'} /><Metric label="Investment grade" value={`${result.summary.investmentGradePercent}%`} note="Of rated instruments" /></section><div className="result-grid"><section className="content-card findings"><div className="section-heading compact"><div><p className="step">REVIEW FINDINGS</p><h2>What needs attention</h2></div><span className="finding-count">{result.findings.length}</span></div>{result.findings.length === 0 ? <Empty text="No material data-quality findings were identified." /> : result.findings.map((finding, index) => <article className="finding" key={`${finding.title}-${index}`}><span className={`severity ${finding.severity.toLowerCase()}`}>{finding.severity}</span><div><small>{finding.category.replaceAll('_',' ')}</small><strong>{finding.title}</strong><p>{finding.detail}</p></div></article>)}</section><section className="content-card recommendation"><p className="step">ANALYST NEXT STEPS</p><h2>{result.summary.decision}</h2><ul>{result.recommendations.map(item => <li key={item}>{item}</li>)}</ul><div className="draft-label">System-generated draft · Human approval required</div></section></div><section className="content-card"><div className="section-heading compact"><div><p className="step">CONSOLIDATED DATA</p><h2>Instrument review</h2></div><span>{result.instruments.length} instruments</span></div><div className="table-wrap"><table><thead><tr><th>ISIN / instrument</th><th>Rating</th><th>Coupon</th><th>Price</th><th>Quantity</th><th>Market value</th><th>Quality</th><th>Sources</th></tr></thead><tbody>{result.instruments.map(item => <tr key={item.isin}><td><strong>{item.isin}</strong><small>{item.issuer ?? 'Issuer not available'}</small></td><td><Status value={item.rating ?? 'N/A'} small /></td><td>{value(item.couponRate, '%')}</td><td>{value(item.marketPrice)}</td><td>{number.format(item.quantity)}</td><td>{money.format(item.marketValue)}</td><td><span className="quality"><i style={{ width: `${item.completeness}%` }} />{item.completeness}%</span></td><td><span className="source-count" title={item.sources.join(', ')}>{item.sources.length}</span></td></tr>)}</tbody></table></div></section><div className="result-grid"><section className="content-card"><p className="step">ISSUER EXPOSURE</p><h2>Uploaded market value</h2><div className="breakdown">{Object.entries(result.issuerExposure).sort((a,b) => b[1]-a[1]).map(([label, amount]) => <div key={label}><span>{label}</span><strong>{money.format(amount)}</strong></div>)}</div></section><section className="content-card"><p className="step">SOURCE PROCESSING</p><h2>Evidence received</h2><div className="breakdown">{result.files.map(file => <div key={file.filename}><span><strong>{file.filename}</strong><small>{file.type} · {file.extractedRows} rows</small></span><Status value={file.status} small /></div>)}</div></section></div></div>;
}

function HistoryView({ items, onOpen }: { items: HistoryItem[]; onOpen: (id: string) => void }) { return <section className="content-card"><div className="section-heading"><div><p className="step">SAVED REVIEWS</p><h2>Analysis history</h2><p>Reopen the exact result produced from each stored upload batch.</p></div></div>{items.length === 0 ? <Empty text="No analysis has been completed yet." /> : <div className="history-list">{items.map(item => <button key={item.id} onClick={() => onOpen(item.id)}><span><strong>{item.name}</strong><small>{dateTime.format(new Date(item.createdAt))} · {item.fileCount} files · {item.instrumentCount} instruments</small></span><span><strong>{money.format(item.totalMarketValue)}</strong><small>{item.dataQualityScore}% completeness</small></span><Status value={item.decision} small /><b>→</b></button>)}</div>}</section>; }
function RegisterView({ bonds }: { bonds: Bond[] }) { return <section className="content-card"><div className="section-heading"><div><p className="step">CENTRAL REGISTER</p><h2>Recognized bonds</h2><p>Valid ISIN records discovered during analysis are added here automatically.</p></div><span>{bonds.length} instruments</span></div>{bonds.length === 0 ? <Empty text="Complete an analysis to populate the bond register." /> : <div className="table-wrap"><table><thead><tr><th>ISIN</th><th>Issuer</th><th>Rating</th><th>Coupon</th><th>Market price</th></tr></thead><tbody>{bonds.map(bond => <tr key={bond.isin}><td><strong>{bond.isin}</strong></td><td>{bond.issuer}</td><td><Status value={bond.rating ?? 'N/A'} small /></td><td>{value(bond.couponRate, '%')}</td><td>{value(bond.marketPrice)}</td></tr>)}</tbody></table></div>}</section>; }
function Metric({ label, value: metricValue, note, tone = '' }: { label: string; value: string; note: string; tone?: string }) { return <article className={`metric ${tone}`}><span>{label}</span><strong>{metricValue}</strong><small>{note}</small></article>; }
function Status({ value: status, small = false }: { value: string; small?: boolean }) { const tone = /READY|COMPLETED|PROCESSED|AAA|AA/.test(status) ? 'positive' : /HOLD|FAILED|HIGH|N\/A/.test(status) ? 'negative' : 'caution'; return <span className={`status ${tone} ${small ? 'small' : ''}`}>{status.replaceAll('_',' ')}</span>; }
function Empty({ text }: { text: string }) { return <div className="empty"><span>◇</span><p>{text}</p></div>; }
function value(input: number | null, suffix = '') { return input == null ? <span className="na">Not available</span> : `${number.format(input)}${suffix}`; }
function extension(name: string) { return name.split('.').pop()?.toLowerCase() ?? 'file'; }
function formatBytes(bytes: number) { return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function viewTitle(view: View) { return ({ new: 'New investment analysis', result: 'Analysis result', history: 'Analysis history', register: 'Bond register' })[view]; }
function Icon({ name }: { name: string }) { return <span className="nav-icon" aria-hidden>{({ upload: '＋', result: '◎', history: '◷', bond: '▤' } as Record<string,string>)[name]}</span>; }

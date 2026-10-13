import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { cmd, useGlass } from '../../lib/engine';
import type { Settings } from '../../lib/types';

const SECTIONS = ['Privacy', 'Student', 'Audio', 'Screen', 'AI models', 'Overlay', 'Shortcuts', 'Diagnostics'] as const;

const SHORTCUT_LABELS: Record<string, string> = {
  toggleOverlay: 'Show / hide overlay', focusContext: 'Type context', expandAnswer: 'Expand answer',
  showTask: 'Show current task', explain: 'Explain current topic', catchup: 'What did I miss?',
  snip: 'Snip & solve', openNotes: 'Open notes', pauseCapture: 'Pause / resume capture',
};

const PROFILE_TEXT: Record<string, { title: string; text: string }> = {
  cool: { title: 'Low heat', text: 'Least work for the laptop: one pass per sentence, no second check, slower background notes. Best for thin laptops or weak cooling.' },
  fast: { title: 'Fast', text: 'Smaller speech model, no second-check pass. Lowest latency and GPU use.' },
  balanced: { title: 'Balanced', text: 'Whisper large-v3-turbo + a background second check that can correct an answer.' },
  quality: { title: 'Quality', text: 'One vision-language model answers everything, so diagrams on screen are read automatically. Wider context and careful decoding.' },
};

export function SettingsView() {
  const s = useGlass();
  const st = s.settings;
  const [section, setSection] = useState<(typeof SECTIONS)[number]>(st?.consent_acknowledged ? 'Audio' : 'Privacy');
  if (!st) return <div className="p-6 text-faint">{s.connected ? 'Loading settings…' : 'Settings are available once the engine is running.'}</div>;
  const patch = (p: Partial<Settings> & Record<string, unknown>) => cmd('settings.set', { patch: p });

  return (
    <div className="h-full grid grid-cols-[180px_1fr] min-h-0">
      <nav className="p-3 border-r border-white/[0.06] space-y-0.5">
        {SECTIONS.map((x) => (
          <button key={x} className={`nav-item w-full ${section === x ? 'active' : ''}`} onClick={() => setSection(x)}>{x}
            {x === 'Privacy' && !st.consent_acknowledged && <span className="ml-auto dot dot-warn" />}</button>
        ))}
      </nav>
      <div className="min-h-0 scroll p-6 max-w-[760px]">
        {section === 'Privacy' && <Privacy st={st} patch={patch} />}
        {section === 'Student' && <Student st={st} patch={patch} />}
        {section === 'Audio' && <Audio st={st} patch={patch} />}
        {section === 'Screen' && <Screen st={st} patch={patch} />}
        {section === 'AI models' && <Models st={st} patch={patch} />}
        {section === 'Overlay' && <OverlayPrefs />}
        {section === 'Shortcuts' && <Shortcuts />}
        {section === 'Diagnostics' && <Diagnostics />}
      </div>
    </div>
  );
}

type P = { st: Settings; patch: (p: any) => Promise<any> };

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-6 py-3 border-b border-white/[0.05]">
      <div className="flex-1">
        <div className="text-[13px]">{label}</div>
        {hint && <div className="text-[11.5px] text-faint mt-0.5 leading-snug">{hint}</div>}
      </div>
      <div className="flex-none flex items-center gap-2 max-w-[340px]">{children}</div>
    </div>
  );
}

function Toggle({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return <button role="switch" aria-checked={on} aria-label={label} className="toggle" onClick={() => onChange(!on)} />;
}

function Privacy({ st, patch }: P) {
  const [agree, setAgree] = useState(false);
  return (
    <div>
      <div className="h-title">Privacy & consent</div>
      <p className="text-[12.5px] text-muted mt-2 leading-relaxed">
        GLASS processes lesson audio and, if you turn it on, the window you choose — entirely on this PC. Nothing is uploaded, raw audio and screenshots are never saved, and capture only runs between <b>Start lesson</b> and <b>End lesson</b>, with the lumen line and the status dot showing it is active.
      </p>
      {!st.consent_acknowledged ? (
        <div className="panel p-4 mt-4">
          <div className="text-[13px] font-semibold">Before the first lesson</div>
          <ul className="text-[12.5px] text-muted mt-2 space-y-1.5 list-disc pl-5 leading-snug">
            <li>Check that your school and teacher allow an AI assistant to process lesson audio and shared content.</li>
            <li>Where required, other participants must be told or have agreed.</li>
            <li>GLASS suggests answers; you decide what to say. It never speaks or types into the meeting for you.</li>
          </ul>
          <label className="flex items-center gap-2 mt-3 text-[12.5px]">
            <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} className="accent-[#a8b4ff]" />
            I have permission to use GLASS in my lessons
          </label>
          <button className="btn btn-primary mt-3" disabled={!agree} onClick={() => patch({ consent_acknowledged: true })}>Confirm and continue</button>
        </div>
      ) : (
        <div className="text-[12.5px] text-exact mt-3">Consent confirmed. <button className="btn btn-ghost h-[22px] text-[11.5px]" onClick={() => patch({ consent_acknowledged: false })}>Withdraw</button></div>
      )}
      <div className="mt-5">
        <Row label="Save lesson transcripts" hint="Needed for the transcript tab and better notes. Text only; audio is never stored.">
          <Toggle on={st.store_transcript !== false} onChange={(v) => patch({ store_transcript: v })} label="Save transcripts" />
        </Row>
        <Row label="Delete lessons automatically" hint="Older lessons (notes, transcript, tasks) are removed when GLASS starts.">
          <select className="field h-[28px] w-[160px]" value={st.retention_days} onChange={(e) => patch({ retention_days: Number(e.target.value) })}>
            <option value={0}>Keep forever</option><option value={7}>After 7 days</option><option value={30}>After 30 days</option><option value={90}>After 90 days</option><option value={365}>After a year</option>
          </select>
        </Row>
        <Row label="Raw audio and screenshots" hint="Never written to disk. Screen frames are analysed in memory and discarded.">
          <span className="chip chip-exact">Not stored</span>
        </Row>
      </div>
    </div>
  );
}

function Student({ st, patch }: P) {
  const [name, setName] = useState(st.student_name);
  const [aliases, setAliases] = useState(st.name_aliases.join(', '));
  return (
    <div>
      <div className="h-title">Student</div>
      <Row label="Your name" hint="When the teacher says it, GLASS raises the answer panel immediately.">
        <input className="field w-[200px]" value={name} onChange={(e) => setName(e.target.value)} onBlur={() => patch({ student_name: name.trim() || st.student_name })} />
      </Row>
      <Row label="How it might be heard" hint="Spellings speech recognition might produce for your name, separated by commas. Similar-sounding names are matched too.">
        <input className="field w-[260px]" value={aliases} onChange={(e) => setAliases(e.target.value)}
          onBlur={() => patch({ name_aliases: aliases.split(',').map((x) => x.trim()).filter(Boolean) })} />
      </Row>
      <Row label="Lesson language" hint="Auto uses English, and automatic language detection in Arabic lessons.">
        <select className="field h-[28px] w-[160px]" value={st.language} onChange={(e) => patch({ language: e.target.value })}>
          <option value="auto">Auto</option><option value="en">English</option><option value="ar">Arabic</option>
        </select>
      </Row>
    </div>
  );
}

function Audio({ st, patch }: P) {
  const s = useGlass();
  const [dev, setDev] = useState<{ loopback: { name: string; default: boolean }[]; input: { name: string; default: boolean }[]; error?: string } | null>(null);
  const load = () => cmd('devices').then(setDev).catch(() => {});
  useEffect(() => { load(); }, []);
  return (
    <div>
      <div className="h-title">Audio</div>
      <Row label="Lesson audio" hint="GLASS listens to what your PC plays (system loopback) — no virtual cables needed. It follows your default output when it changes.">
        <select className="field h-[28px] w-[260px]" value={st.audio_device || ''} onChange={(e) => patch({ audio_device: e.target.value || null })}>
          <option value="">Default output device</option>
          {dev?.loopback.map((d) => <option key={d.name} value={d.name}>{d.name}{d.default ? ' (default)' : ''}</option>)}
        </select>
        <button className="btn btn-icon" onClick={load} title="Refresh devices"><RefreshCw size={12} /></button>
      </Row>
      <Row label="Also transcribe my microphone" hint="Off by default. Lets GLASS notice when you've answered. Never turns on by itself.">
        <Toggle on={st.mic_enabled} onChange={(v) => patch({ mic_enabled: v })} label="Microphone" />
      </Row>
      {st.mic_enabled && (
        <Row label="Microphone">
          <select className="field h-[28px] w-[260px]" value={st.mic_device || ''} onChange={(e) => patch({ mic_device: e.target.value || null })}>
            <option value="">Default microphone</option>
            {dev?.input.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
          </select>
        </Row>
      )}
      <Row label="Capture status">
        <span className="text-[12px] text-muted">{s.session ? `${s.status?.audio.state}${s.status?.audio.device ? ` · ${s.status.audio.device}` : ''}` : 'Starts with the lesson'}</span>
      </Row>
      {dev?.error && <div className="text-[12px] text-unsure mt-2">Could not list devices: {dev.error}</div>}
    </div>
  );
}

function Screen({ st, patch }: P) {
  const [win, setWin] = useState<{ windows: { hwnd: number; title: string; exe: string; meet: boolean }[]; monitors: { index: number; w: number; h: number }[] } | null>(null);
  const load = () => cmd('windows').then(setWin).catch(() => {});
  useEffect(() => { load(); }, []);
  const src = st.screen_source;
  const value = src?.kind === 'window' ? `w:${src.hwnd}` : src?.kind === 'monitor' ? `m:${src.monitor}` : '';
  const zones = st.exclusion_zones || [];
  return (
    <div>
      <div className="h-title">Screen</div>
      <Row label="Read the screen during lessons" hint="Lets GLASS see slides, worksheets and equations. Only the source below is captured; other windows are never included.">
        <Toggle on={st.screen_enabled} onChange={(v) => patch({ screen_enabled: v })} label="Screen reading" />
      </Row>
      <Row label="What to read" hint="Pick the Google Meet window for best results.">
        <select className="field h-[28px] w-[280px]" value={value} onChange={(e) => {
          const v = e.target.value;
          if (v.startsWith('w:')) { const w = win?.windows.find((x) => String(x.hwnd) === v.slice(2)); patch({ screen_source: { kind: 'window', hwnd: w?.hwnd, title: w?.title, meet: w?.meet } }); }
          else if (v.startsWith('m:')) patch({ screen_source: { kind: 'monitor', monitor: Number(v.slice(2)) } });
        }}>
          <option value="" disabled>Choose…</option>
          {win?.windows.map((w) => <option key={w.hwnd} value={`w:${w.hwnd}`}>{w.meet ? '★ ' : ''}{w.title.slice(0, 60)}</option>)}
          {win?.monitors.map((m) => <option key={m.index} value={`m:${m.index}`}>Entire screen {m.index} ({m.w}×{m.h})</option>)}
        </select>
        <button className="btn btn-icon" onClick={load} title="Refresh"><RefreshCw size={12} /></button>
      </Row>
      <Row label="Focus area" hint="Ignore the edges of the window (browser tabs, Meet controls, participant tiles).">
        <select className="field h-[28px] w-[220px]" value={JSON.stringify(st.screen_crop)} onChange={(e) => patch({ screen_crop: JSON.parse(e.target.value) })}>
          <option value="null">Whole window</option>
          <option value="[0,0.12,1,0.76]">Skip browser bar and Meet controls</option>
          <option value="[0,0.12,0.78,0.76]">…and the side panel</option>
        </select>
      </Row>
      <Row label="Text recognition" hint="Windows OCR is fastest; maths lines are double-checked with Tesseract either way.">
        <select className="field h-[28px] w-[160px]" value={st.ocr_engine} onChange={(e) => patch({ ocr_engine: e.target.value })}>
          <option value="windows">Windows OCR</option><option value="tesseract">Tesseract</option>
        </select>
      </Row>
      <div className="mt-4">
        <div className="text-[13px]">Hidden areas</div>
        <div className="text-[11.5px] text-faint mt-0.5">Blacked out before anything is read — e.g. a chat panel or another student's details. Values are percentages of the source.</div>
        <div className="space-y-1.5 mt-2">
          {zones.map((z, i) => (
            <div key={i} className="flex items-center gap-1.5">
              {['left', 'top', 'width', 'height'].map((k, j) => (
                <input key={k} aria-label={k} className="field w-[72px] h-[28px]" type="number" min={0} max={100} value={Math.round(z[j] * 100)}
                  onChange={(e) => { const nz = zones.map((x) => [...x]); nz[i][j] = Number(e.target.value) / 100; patch({ exclusion_zones: nz }); }} />
              ))}
              <button className="btn btn-ghost btn-danger" onClick={() => patch({ exclusion_zones: zones.filter((_, k) => k !== i) })}>Remove</button>
            </div>
          ))}
          <button className="btn" onClick={() => patch({ exclusion_zones: [...zones, [0.78, 0, 0.22, 1]] })}>Add hidden area</button>
        </div>
      </div>
    </div>
  );
}

function Models({ st, patch }: P) {
  const s = useGlass();
  const [models, setModels] = useState<{ name: string; size: number }[]>([]);
  useEffect(() => { cmd('models.list').then(setModels).catch(() => {}); }, [s.status?.llm.state]);
  const llm = s.status?.llm, vis = s.status?.vision, asr = s.status?.asr;
  const m = s.metrics;
  const eff = st.effective || {};
  const statusLine = (x: any) => !x ? '—' : x.state === 'downloading' ? `Downloading ${Math.round((x.progress || 0) * 100)}%` : `${x.state}${x.model ? ` · ${x.model}` : ''}${x.device ? ` · ${x.device}` : ''}${x.message ? ` — ${x.message}` : ''}`;
  return (
    <div>
      <div className="h-title">AI models & performance</div>
      <p className="text-[12.5px] text-muted mt-2 leading-relaxed">Everything runs locally on your GPU through Ollama and faster-whisper. No API keys, no subscriptions; maths is always computed exactly with SymPy.</p>
      <div className="grid grid-cols-2 gap-2 mt-4">
        {(['cool', 'fast', 'balanced', 'quality'] as const).map((p) => (
          <button key={p} className={`panel p-3 text-left ${st.profile === p ? 'ring-1 ring-lumen/60 bg-white/[0.05]' : 'hover:bg-white/[0.05]'}`} onClick={() => patch({ profile: p })}>
            <div className="text-[13px] font-semibold">{PROFILE_TEXT[p].title}</div>
            <div className="text-[11.5px] text-faint mt-1 leading-snug">{PROFILE_TEXT[p].text}</div>
          </button>
        ))}
      </div>
      <Row label="Speech recognition" hint={`Model: ${eff.asr_model}`}><span className="text-[12px] text-muted">{statusLine(asr)}</span></Row>
      <Row label="Answer model" hint="Any model installed in Ollama. Instruct (non-thinking) models answer fastest.">
        <select className="field h-[28px] w-[220px]" value={eff.llm_model} onChange={(e) => patch({ overrides: { ...st.overrides, llm_model: e.target.value } })}>
          {[...new Set([eff.llm_model, ...models.map((x) => x.name)])].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </Row>
      <Row label="Status"><span className={`text-[12px] ${llm?.state === 'ready' ? 'text-exact' : 'text-called'}`}>{statusLine(llm)}</span>
        {llm?.state === 'missing' && <button className="btn btn-primary" onClick={() => cmd('models.pull', { model: llm.model })}>Download</button>}
        {(llm?.state === 'unavailable' || llm?.state === 'error') && <button className="btn" onClick={() => cmd('llm.check')}>Retry</button>}
      </Row>
      <Row label="Vision model" hint="Reads diagrams, graphs and handwriting in Snip & solve (and automatically in Quality).">
        <select className="field h-[28px] w-[220px]" value={st.vision_model} onChange={(e) => patch({ vision_model: e.target.value })}>
          {[...new Set([st.vision_model, ...models.map((x) => x.name).filter((n) => /vl|vision|llava|gemma3|minicpm/i.test(n))])].map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
        {vis?.state === 'missing' && <button className="btn btn-primary" onClick={() => cmd('models.pull', { model: st.vision_model })}>Download</button>}
      </Row>
      <div className="eyebrow mt-6 mb-2">Measured on this PC</div>
      <div className="grid grid-cols-3 gap-2">
        <Stat label="Answer ready (avg)" value={m.answer_say_ms_avg ? `${(m.answer_say_ms_avg / 1000).toFixed(2)} s` : '—'} />
        <Stat label="LLM first token" value={m.llm_first_token_ms ? `${m.llm_first_token_ms} ms` : '—'} />
        <Stat label="LLM speed" value={m.llm_tok_s ? `${m.llm_tok_s} tok/s` : '—'} />
        <Stat label="Speech → text" value={m.asr_latency_ms ? `${m.asr_latency_ms} ms` : '—'} />
        <Stat label="Whisper real-time factor" value={m.asr_rtf ? `${m.asr_rtf}×` : '—'} />
        <Stat label="OCR" value={m.ocr_ms ? `${m.ocr_ms} ms` : '—'} />
        <Stat label="GPU" value={m.gpu ? `${m.gpu_util}% · ${((m.vram_used_mb || 0) / 1024).toFixed(1)} GB VRAM` : 'not detected'} />
        <Stat label="CPU / RAM" value={m.cpu != null ? `${Math.round(m.cpu)}% · ${m.ram_used_gb}/${m.ram_total_gb} GB` : '—'} />
        <Stat label="Queue" value={m.scheduler ? `${m.scheduler.running || 'idle'}${m.scheduler.queued.length ? ` +${m.scheduler.queued.length}` : ''}` : '—'} />
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="panel p-2.5"><div className="text-[11px] text-faint">{label}</div><div className="mono text-[12.5px] mt-0.5">{value}</div></div>;
}

function OverlayPrefs() {
  const [ui, setUi] = useState<any>(null);
  useEffect(() => { window.glass.invoke('ui:get').then(setUi); }, []);
  if (!ui) return null;
  const set = (p: any) => window.glass.invoke('ui:set', p).then((r: any) => setUi(r.ui));
  return (
    <div>
      <div className="h-title">Overlay</div>
      <Row label="Opacity" hint="Lower it if the panel covers lesson content you need to see.">
        <input type="range" min={35} max={100} value={Math.round((ui.opacity || 1) * 100)} onChange={(e) => set({ opacity: Number(e.target.value) / 100 })} className="w-[180px] accent-[#a8b4ff]" />
        <span className="mono text-[11.5px] text-muted w-10">{Math.round((ui.opacity || 1) * 100)}%</span>
      </Row>
      <Row label="Glass style" hint="Acrylic blurs whatever is behind the panel (Windows 11). Solid tint is more legible over busy slides.">
        <select className="field h-[28px] w-[160px]" value={ui.glass} onChange={(e) => set({ glass: e.target.value })}>
          <option value="acrylic">Acrylic blur</option><option value="tint">Solid tint</option>
        </select>
      </Row>
      <Row label="Click-through when minimised" hint="The small pill lets clicks pass to the lesson underneath. Expand it again with the show/hide shortcut, or when you're asked a question.">
        <button role="switch" aria-checked={!!ui.ghost} aria-label="Click-through pill" className="toggle" onClick={() => set({ ghost: !ui.ghost })} />
      </Row>
      <Row label="Position" hint="Drag the overlay by its top bar; GLASS remembers where you put it.">
        <button className="btn" onClick={() => set({ overlay: null })}>Reset position</button>
      </Row>
    </div>
  );
}

function Shortcuts() {
  const [ui, setUi] = useState<any>(null);
  const [capturing, setCapturing] = useState<string | null>(null);
  const [failed, setFailed] = useState<string[]>([]);
  useEffect(() => { window.glass.invoke('ui:get').then(setUi); }, []);
  useEffect(() => {
    if (!capturing) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      if (e.key === 'Escape') { setCapturing(null); return; }
      if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return;
      const mods = [e.ctrlKey && 'Control', e.altKey && 'Alt', e.shiftKey && 'Shift', e.metaKey && 'Super'].filter(Boolean);
      if (!mods.length) return;
      const key = e.key === ' ' ? 'Space' : e.key.length === 1 ? e.key.toUpperCase() : e.key;
      window.glass.invoke('ui:set', { shortcuts: { [capturing]: [...mods, key].join('+') } }).then((r: any) => { setUi(r.ui); setFailed(r.failed); });
      setCapturing(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [capturing]);
  if (!ui) return null;
  return (
    <div>
      <div className="h-title">Shortcuts</div>
      <p className="text-[12.5px] text-muted mt-2">They work everywhere, even while Google Meet is focused. Click one, then press the new combination (with Ctrl, Alt or Shift).</p>
      {failed.length > 0 && <div className="text-[12px] text-unsure mt-2">Another app already uses {failed.join(', ')} — pick a different combination.</div>}
      <div className="mt-3">
        {Object.entries(SHORTCUT_LABELS).map(([k, label]) => (
          <Row key={k} label={label}>
            <button className={`kbd h-[26px] px-3 ${capturing === k ? 'ring-1 ring-lumen' : ''}`} onClick={() => setCapturing(k)}>
              {capturing === k ? 'Press keys…' : (ui.shortcuts[k] || 'Not set').replace(/Control/g, 'Ctrl')}
            </button>
            <button className="btn btn-ghost h-[24px] text-[11px]" onClick={() => window.glass.invoke('ui:set', { shortcuts: { [k]: ui.defaults[k] } }).then((r: any) => setUi(r.ui))}>Default</button>
          </Row>
        ))}
      </div>
    </div>
  );
}

function Diagnostics() {
  const s = useGlass();
  const [log, setLog] = useState('');
  const load = () => window.glass.invoke<string>('engine:log').then(setLog);
  useEffect(() => { load(); }, []);
  return (
    <div>
      <div className="h-title">Diagnostics</div>
      <Row label="Engine" hint={`Process: ${s.proc.state}${s.proc.message ? ` — ${s.proc.message}` : ''}`}>
        <button className="btn" onClick={() => window.glass.invoke('engine:restart')}>Restart engine</button>
      </Row>
      <div className="grid grid-cols-2 gap-2 mt-3">
        {s.status && Object.entries(s.status).map(([k, v]) => (
          <div key={k} className="panel p-2.5"><div className="eyebrow">{k}</div><div className="text-[12px] text-muted mt-0.5 break-words">{v.state}{v.model ? ` · ${v.model}` : ''}{v.device ? ` · ${String(v.device)}` : ''}{v.message ? ` — ${v.message}` : ''}</div></div>
        ))}
      </div>
      <div className="flex items-center mt-5 mb-1"><span className="eyebrow">Engine log</span><button className="btn btn-ghost h-[22px] ml-auto text-[11px]" onClick={load}>Refresh</button></div>
      <pre className="panel p-3 text-[11px] mono text-muted whitespace-pre-wrap max-h-[360px] scroll selectable">{log.slice(-12000) || '(empty)'}</pre>
    </div>
  );
}

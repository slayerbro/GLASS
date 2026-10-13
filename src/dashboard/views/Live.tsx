import { useEffect, useRef, useState } from 'react';
import { Eye, EyeOff, Mic } from 'lucide-react';
import { cmd, fmtClock, subjectName, useGlass } from '../../lib/engine';
import { AnswerCard, type Level } from '../../overlay/AnswerCard';

export function LiveView() {
  const s = useGlass();
  const subj = subjectName(s.lesson.subject_id, s.settings);
  return (
    <div className="h-full grid grid-cols-[minmax(240px,1fr)_minmax(340px,1.35fr)_minmax(240px,0.95fr)] min-h-0">
      <section className="min-h-0 flex flex-col border-r border-white/[0.06]">
        <Header title="Transcript" right={<AudioState />} />
        <Transcript />
      </section>
      <section className="min-h-0 flex flex-col border-r border-white/[0.06]">
        <div className="px-5 pt-4 pb-3 border-b border-white/[0.06]">
          <div className="eyebrow">{subj || 'Subject not detected yet'}</div>
          <div className="h-title mt-0.5">{s.lesson.topic || (s.session ? 'Listening for the topic…' : 'No lesson running')}</div>
          {s.lesson.summary && <div className="text-[12.5px] text-muted mt-1">{s.lesson.summary}</div>}
        </div>
        <Answers />
      </section>
      <section className="min-h-0 flex flex-col">
        <Header title="Screen" right={<ScreenState />} />
        <div className="flex-1 min-h-0 scroll p-4 space-y-4">
          <ScreenPanel />
          {s.task && (
            <div className="panel p-3">
              <div className="eyebrow mb-1">Current task</div>
              <div className="text-[13px] font-semibold">{s.task.description}</div>
              <div className="text-[12px] text-muted">{[s.task.source, s.task.questions && `Q ${s.task.questions}`, s.task.time_minutes && `${s.task.time_minutes} min`].filter(Boolean).join(' · ')}</div>
            </div>
          )}
          <Formulas />
          <Health />
        </div>
      </section>
    </div>
  );
}

function Header({ title, right }: { title: string; right?: React.ReactNode }) {
  return (
    <div className="h-11 px-4 flex items-center border-b border-white/[0.06] flex-none">
      <span className="eyebrow">{title}</span>
      <span className="ml-auto">{right}</span>
    </div>
  );
}

function AudioState() {
  const s = useGlass();
  const a = s.status?.audio;
  if (!s.session) return <span className="text-[11.5px] text-faint">Off</span>;
  if (a?.state === 'running') return <span className="text-[11.5px] text-muted flex items-center gap-1.5"><span className={`dot ${s.speaking ? 'dot-live' : ''}`} />{String(a.device || 'System audio').slice(0, 28)}</span>;
  return <span className="text-[11.5px] text-called">{a?.state === 'error' ? `Audio error: ${a.message}` : a?.state || '…'}</span>;
}

function Transcript() {
  const s = useGlass();
  const ref = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);
  useEffect(() => { if (stick && ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [s.transcript.length, s.partial?.text, stick]);
  const asr = s.status?.asr;
  return (
    <div ref={ref} className="flex-1 min-h-0 scroll px-4 py-3 space-y-2.5 selectable"
      onScroll={(e) => { const el = e.currentTarget; setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 40); }}>
      {!s.transcript.length && !s.partial && (
        <div className="text-[12.5px] text-faint mt-6 text-center leading-relaxed px-4">
          {!s.session ? 'Start a lesson to see the live transcript here.' :
            asr?.state !== 'ready' ? `Speech recognition: ${asr?.state}` : 'No speech detected yet. Play the lesson audio on this PC.'}
        </div>
      )}
      {s.transcript.map((u) => (
        <div key={u.id} className="text-[13px] leading-relaxed">
          <span className="mono text-[10.5px] text-faint mr-2">{fmtClock(u.t0)}</span>
          {u.source === 'me' && <span className="chip h-[16px] px-1.5 text-[10px] mr-1.5">you</span>}
          <span className={u.noise ? 'text-faint' : u.source === 'me' ? 'text-muted' : 'text-[#dfe3ec]'} title={u.noise ? 'Not lesson content — kept out of the AI context' : undefined}>{u.text}</span>
        </div>
      ))}
      {s.partial && (
        <div className="text-[13px] leading-relaxed text-muted italic"><span className="mono text-[10.5px] text-faint mr-2 not-italic">now</span>{s.partial.text}</div>
      )}
    </div>
  );
}

function Answers() {
  const s = useGlass();
  const [levels, setLevels] = useState<Record<string, Level>>({});
  const [text, setText] = useState('');
  const ids = [...s.order].reverse().filter((id) => s.answers[id] && s.answers[id].status !== 'superseded');
  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="p-3 border-b border-white/[0.06] flex gap-2">
        <input className="field" placeholder="Ask about the lesson, or type a question number…" value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && text.trim()) { cmd('ask', { text: text.trim() }); setText(''); } }} />
        <button className="btn whitespace-nowrap" disabled={!s.session} onClick={() => cmd("explain")}>Explain that</button>
        <button className="btn whitespace-nowrap" disabled={!s.session} onClick={() => cmd("catchup", { minutes: 5 })}>Catch me up</button>
      </div>
      <div className="flex-1 min-h-0 scroll p-3 space-y-2.5">
        {!ids.length && (
          <div className="text-[12.5px] text-faint text-center mt-10 leading-relaxed px-8">
            Questions the teacher asks appear here with a suggested answer. You can also ask anything above — GLASS uses what was said and shown in the lesson.
          </div>
        )}
        {ids.map((id) => {
          const a = s.answers[id];
          return (
            <div key={id} className={`glass-panel ${a.directed ? 'called-ring' : ''} ${a.rhetorical ? 'opacity-55' : ''}`} style={{ background: 'rgba(255,255,255,0.035)' }}>
              <AnswerCard a={a} level={levels[id] || 'quick'} setLevel={(l) => setLevels({ ...levels, [id]: l })} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ScreenState() {
  const s = useGlass();
  const st = s.status?.screen.state;
  const on = s.settings?.screen_enabled;
  return (
    <button className="btn h-[24px] text-[11.5px]" onClick={() => cmd('settings.set', { patch: { screen_enabled: !on } })}
      title={on ? 'Stop reading the screen' : 'Read the selected window or screen'}>
      {on ? <Eye size={12} /> : <EyeOff size={12} />}{on ? (st === 'watching' ? 'Watching' : st === 'paused' ? 'Paused' : st || 'On') : 'Off'}
    </button>
  );
}

function ScreenPanel() {
  const s = useGlass();
  const sc = s.screen;
  const src = s.settings?.screen_source;
  if (!s.settings?.screen_enabled) {
    return <div className="text-[12px] text-faint leading-relaxed">Screen reading is off. Turn it on to let GLASS connect “question 3” to what's on the slide. Choose the window in Settings.</div>;
  }
  if (s.status?.screen.state === 'unavailable') return <div className="text-[12px] text-called">{String(s.status.screen.message)}</div>;
  if (!sc) return <div className="text-[12px] text-faint">Waiting for the first readable frame of {src?.title || 'the screen'}…</div>;
  const items = Object.entries(sc.items);
  return (
    <div className="space-y-3">
      <div className="text-[11px] text-faint">{sc.title} · read at {fmtClock(sc.t)} · OCR {sc.ocr_ms} ms</div>
      {items.length > 0 && (
        <div>
          <div className="eyebrow mb-1">Questions on screen</div>
          <ol className="space-y-1">
            {items.map(([n, t]) => (
              <li key={n} className="text-[12.5px] flex gap-2 group">
                <span className="mono text-faint w-5 flex-none">{n}.</span>
                <span className="flex-1 whitespace-pre-wrap selectable">{t}</span>
                <button className="btn h-[20px] text-[10.5px] opacity-0 group-hover:opacity-100" onClick={() => cmd('ask', { text: `question ${n}` })}>Solve</button>
              </li>
            ))}
          </ol>
        </div>
      )}
      {sc.equations.length > 0 && (
        <div>
          <div className="eyebrow mb-1">Equations seen</div>
          <div className="flex flex-wrap gap-1">{sc.equations.map((e) => <span key={e} className="chip mono">{e}</span>)}</div>
        </div>
      )}
      <details>
        <summary className="text-[11.5px] text-muted cursor-default">All text read from the screen</summary>
        <pre className="mt-1 text-[11.5px] text-muted whitespace-pre-wrap mono selectable max-h-60 scroll">{sc.text || '(no text found)'}</pre>
      </details>
    </div>
  );
}

function Formulas() {
  const s = useGlass();
  const f = s.notes?.formulas || [];
  if (!f.length) return null;
  return (
    <div>
      <div className="eyebrow mb-1">Formulas this lesson</div>
      <div className="space-y-1">
        {f.map((x) => (
          <div key={x.formula} className="text-[12px]"><span className="mono text-lumen">{x.formula}</span> <span className="text-faint">— {x.meaning}</span></div>
        ))}
      </div>
    </div>
  );
}

function Health() {
  const m = useGlass((s) => s.metrics);
  const rows: [string, string][] = [
    ['Answer (avg)', m.answer_say_ms_avg ? `${(m.answer_say_ms_avg / 1000).toFixed(1)} s` : '—'],
    ['Speech → text', m.asr_latency_ms ? `${m.asr_latency_ms} ms` : '—'],
    ['First token', m.llm_first_token_ms ? `${m.llm_first_token_ms} ms` : '—'],
    ['Generation', m.llm_tok_s ? `${m.llm_tok_s} tok/s` : '—'],
    ['GPU', m.gpu_util != null ? `${m.gpu_util}%` : '—'],
    ['CPU', m.cpu != null ? `${Math.round(m.cpu)}%` : '—'],
  ];
  return (
    <div>
      <div className="eyebrow mb-1 flex items-center gap-1"><Mic size={11} />Measured performance</div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between text-[11.5px]"><span className="text-faint">{k}</span><span className="mono text-muted">{v}</span></div>
        ))}
      </div>
    </div>
  );
}

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Thermometer, Camera, Eye, LayoutDashboard, Minus, Pause, Play, Square, CornerDownLeft, ScanText, History, Lightbulb, NotebookPen, ListTodo } from 'lucide-react';
import { addStudyText, cmd, connectEngine, dismissAsk, readiness, subjectName, useGlass } from '../lib/engine';
import type { Answer } from '../lib/types';
import { AnswerCard, type Level } from './AnswerCard';
import { TaskCard } from './TaskCard';

const W = 380;
const spring = { type: 'spring' as const, stiffness: 520, damping: 42, mass: 0.8 };

type View = 'auto' | 'task' | 'pill';

export function Overlay() {
  const s = useGlass();
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [view, setView] = useState<View>('auto');
  const [level, setLevel] = useState<Level>('quick');
  const [input, setInput] = useState('');
  const [inputMode, setInputMode] = useState<'context' | 'attempt'>('context');
  // Ask = a new question about the lesson; Fix = context / corrections for the answer on screen
  const [tab, setTab] = useState<'ask' | 'fix'>('ask');
  const [tabTouched, setTabTouched] = useState(false);
  const [hint, setHint] = useState('');
  const [subjectPick, setSubjectPick] = useState<string>('');
  const [ghost, setGhost] = useState(false);
  const ready = readiness(s);

  useEffect(() => {
    connectEngine();
    window.glass.invoke('ui:get').then((u: any) => setGhost(!!u?.ghost));
    return window.glass.on('ui:changed', (u: any) => setGhost(!!u?.ghost));
  }, []);

  // the answer to show: newest non-rhetorical card from the last few minutes
  const answer: Answer | null = useMemo(() => {
    const id = s.focusAnswer;
    const a = id ? s.answers[id] : null;
    if (!a || a.rhetorical || a.status === 'superseded') return null;
    if (Date.now() / 1000 - a.t > 240 && !a.waiting) return null;
    return a;
  }, [s.focusAnswer, s.answers]);

  useEffect(() => { setLevel('quick'); }, [s.focusAnswer]);
  // follow the situation unless the student picked a tab: a fresh class question -> Fix, otherwise Ask
  useEffect(() => {
    if (!tabTouched) setTab(answer && answer.mode === 'question' && answer.status !== 'error' ? 'fix' : 'ask');
  }, [answer?.id, tabTouched]);

  // what fills the panel
  let body: 'status' | 'start' | 'answer' | 'task' | 'passive' = 'passive';
  if (!ready.ok) body = 'status';
  else if (!s.session) body = s.focusAnswer && answer ? 'answer' : 'start';
  else if (view === 'task' && s.task) body = 'task';
  else if (s.mode === 'task' && s.task && (!answer || ((s.task as any).updated_at ?? s.task.created_at) > answer.t)) body = 'task';
  else if (answer && (s.mode === 'direct' || s.mode === 'question' || s.mode === 'confusion' || answer.mode !== 'question' || answer.directed || answer.waiting)) body = 'answer';
  else if (s.mode === 'task' && s.task) body = 'task';
  const pill = view === 'pill' || (!!s.session && s.mode === 'idle' && body === 'passive' && !input);
  const called = !!(answer?.directed && body === 'answer' && answer.status !== 'error');

  // ---- window sizing: the native window always hugs the content
  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    // throttled: spring animations would otherwise resize the native window every frame
    let last = { w: 0, h: 0 }, timer: ReturnType<typeof setTimeout> | null = null;
    const send = () => {
      timer = null;
      const r = el.getBoundingClientRect();
      const w = pill ? Math.ceil(r.width) : W, h = Math.ceil(r.height);
      if (Math.abs(w - last.w) < 1 && Math.abs(h - last.h) < 1) return;
      last = { w, h };
      window.glass.send('overlay:resize', { width: w, height: h });
    };
    const ro = new ResizeObserver(() => { if (!timer) timer = setTimeout(send, 60); });
    ro.observe(el);
    send();
    return () => { ro.disconnect(); if (timer) clearTimeout(timer); };
  }, [pill]);

  // ---- main-process events
  useEffect(() => {
    const offs = [
      window.glass.on('shortcut', (action: string) => {
        if (action === 'focusContext') { setView('auto'); setTimeout(() => inputRef.current?.focus(), 30); }
        if (action === 'toggleOverlay') setView((v) => { if (v === 'pill') return 'auto'; window.glass.send('overlay:hide'); return v; });
        if (action === 'expandAnswer') setLevel((l) => (l === 'quick' ? 'explain' : l === 'explain' ? 'detailed' : 'quick'));
        if (action === 'showTask') setView((v) => (v === 'task' ? 'auto' : 'task'));
        if (action === 'explain') { setView('auto'); cmd('explain').catch(flash); }
        if (action === 'catchup') { setView('auto'); cmd('catchup', { minutes: 3 }).catch(flash); }
        if (action === 'pauseCapture') { const sess = useGlass.getState().session; if (sess) cmd('session.pause', { paused: !sess.paused }).catch(flash); }
      }),
      window.glass.on('snip:rect', (r) => { setView('auto'); cmd('snip', r).catch(flash); }),
      window.glass.on('snip:vision', () => { cmd('vision.prepare').catch(() => {}); }),
      window.glass.on('snip:freeze', (p: { x: number; y: number }) => {
        cmd<{ image: string } | null>('snip.freeze', p)
          .then((r) => window.glass.send('snip:frozen', r?.image ?? null))
          .catch(() => window.glass.send('snip:frozen', null));
      }),
      window.glass.on('overlay:bounds', (rect: number[]) => { cmd('overlay.bounds', { rects: [rect] }).catch(() => {}); }),
    ];
    return () => offs.forEach((f) => f());
  }, []);

  // ghost pill: clicks pass straight through to the lesson underneath
  useEffect(() => {
    window.glass.send('overlay:ignore-mouse', pill && ghost);
  }, [pill, ghost]);

  // expanding when the student is called on, even from the pill
  useEffect(() => { if (called && view === 'pill') setView('auto'); }, [called]);
  useEffect(() => {
    if (s.task && s.mode === 'task' && view === 'pill') setView('auto');
  }, [s.task?.id]);
  // detailed answer requested via shortcut
  useEffect(() => {
    if (level === 'detailed' && answer && !answer.detailed && answer.say) cmd('answer.expand', { id: answer.id }).catch(() => {});
  }, [level]);

  function flash(e: Error) { setHint(e.message); setTimeout(() => setHint(''), 4000); }

  async function submit() {
    const text = input.trim();
    if (!text) return;
    setInput('');
    try {
      if (inputMode === 'attempt') await cmd('tutor', { mode: 'check', attempt: text, task: s.task });
      else if (tab === 'ask') await cmd('ask', { text });
      else await cmd('context', { text });
    } catch (e) { flash(e as Error); }
    setInputMode('context');
    setTabTouched(false);
    inputRef.current?.blur();
  }

  async function start() {
    if (!s.settings?.consent_acknowledged) { window.glass.send('dashboard:open', 'settings'); return; }
    try { await cmd('session.start', { subject_id: subjectPick || null }); } catch (e) { flash(e as Error); }
  }

  const listening = !!s.session && !s.session.paused;
  const screenOn = s.status?.screen.state === 'watching';
  const subj = subjectName(s.lesson.subject_id ?? s.session?.subject_id, s.settings);
  const lumenClass = called ? 'called' : !s.session ? 'idle' : s.session.paused ? 'paused' : '';
  const level01 = s.speaking ? 0.85 : 0.12;
  const placeholder = inputMode === 'attempt' ? 'Type your answer or working to check…'
    : tab === 'ask' ? 'Ask anything about the lesson…'
      : answer && (answer.conf === 'low' || answer.unsure) ? 'What is the teacher referring to?'
        : 'Context, or "answer it", "shorter", "quotes"…';

  if (pill) {
    return (
      <div ref={rootRef} className="inline-block" style={{ ['--level' as any]: level01 }}>
        <motion.div layout transition={spring} className={`glass-panel drag h-[34px] px-3 flex items-center gap-2 w-max ${ghost ? 'opacity-60' : ''}`}
          onDoubleClick={() => setView('auto')}>
          <div className={`lumen ${lumenClass}`} />
          <span className="wordmark">GLASS</span>
          <span className={`dot ${listening ? 'dot-live' : 'dot-warn'}`} />
          <span className="text-[11.5px] text-muted max-w-[160px] truncate">{s.session?.paused ? 'Paused' : s.lesson.topic || subj || 'Listening'}</span>
          {screenOn && <Eye size={12} className="text-muted" aria-label="Screen analysis on" />}
          <button className="btn btn-ghost btn-icon h-[22px] w-[22px] no-drag" title="Expand" onClick={() => setView('auto')}>
            <LayoutDashboard size={12} />
          </button>
        </motion.div>
      </div>
    );
  }

  return (
    <div ref={rootRef} style={{ width: W, ['--level' as any]: level01 }}>
      <motion.div layout transition={spring} className={`glass-panel ${called ? 'called-ring' : ''}`}>
        <div className={`lumen ${lumenClass}`} />

        {/* header — drag handle */}
        <div className="drag flex items-center gap-2 h-[34px] pl-4 pr-1.5">
          <span className="wordmark">GLASS</span>
          {subj && <span className="text-[11.5px] text-muted truncate max-w-[110px]">· {subj}</span>}
          <span className="flex items-center gap-1.5 text-[11.5px] text-faint ml-1">
            <span className={`dot ${!ready.ok ? 'dot-err' : listening ? 'dot-live' : s.session ? 'dot-warn' : ''}`} />
            {!ready.ok ? 'Not ready' : !s.session ? 'Ready' : s.session.paused ? 'Paused' : s.speaking ? 'Listening' : 'Listening'}
          </span>
          {screenOn && <span title="Screen analysis is on" className="text-muted"><Eye size={12} /></span>}
          {s.settings?.mic_enabled && s.session && <span className="chip h-[16px] px-1.5 text-[10px]" title="Your microphone is being transcribed">mic</span>}
          <div className="ml-auto flex items-center no-drag">
            {s.session && (
              <button className="btn btn-ghost btn-icon" title={s.session.paused ? 'Resume capture' : 'Pause capture'}
                onClick={() => cmd('session.pause', { paused: !s.session!.paused }).catch(flash)}>
                {s.session.paused ? <Play size={13} /> : <Pause size={13} />}
              </button>
            )}
            <button className="btn btn-ghost btn-icon" title="Open dashboard" onClick={() => window.glass.send('dashboard:open', 'live')}><LayoutDashboard size={13} /></button>
            {s.session && <button className="btn btn-ghost btn-icon" title="Shrink to pill" onClick={() => setView('pill')}><Minus size={13} /></button>}
          </div>
        </div>

        <ThermalNotice />
        <RegisterNote />
        <GlassAsks onError={flash} />

        <AnimatePresence mode="popLayout" initial={false}>
          <motion.div key={body + (body === 'answer' ? answer?.id : '')} layout initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }} transition={spring}>
            {body === 'status' && <StatusBody text={ready.text} detail={ready.detail} />}
            {body === 'start' && (
              <div className="px-4 pb-3 pt-1">
                <div className="say text-[15px]">Ready when class starts.</div>
                <div className="text-[12px] text-muted mt-0.5">GLASS listens to the lesson audio{s.settings?.screen_enabled ? ' and reads your selected window' : ''} only after you start.</div>
                <div className="mt-2.5 flex gap-1.5">
                  <select className="field h-[28px] flex-1" value={subjectPick} onChange={(e) => setSubjectPick(e.target.value)} aria-label="Subject">
                    <option value="">Detect subject automatically</option>
                    {s.settings?.subjects.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
                  </select>
                  <button className="btn btn-primary h-[28px]" onClick={start}>
                    {s.settings?.consent_acknowledged ? 'Start lesson' : 'Set up capture'}
                  </button>
                </div>
              </div>
            )}
            {body === 'answer' && answer && <AnswerCard a={answer} level={level} setLevel={setLevel} />}
            {body === 'task' && s.task && (
              <TaskCard task={s.task} settings={s.settings}
                onTutor={(mode) => { setView('auto'); cmd('tutor', { mode, task: s.task }).catch(flash); }}
                onAttempt={() => { setInputMode('attempt'); setTimeout(() => inputRef.current?.focus(), 20); }} />
            )}
            {body === 'passive' && <PassiveBody />}
          </motion.div>
        </AnimatePresence>

        {/* instant context + quick actions */}
        {ready.ok && (
          <div className="px-2.5 pb-2.5 pt-0.5">
            <div className="relative flex items-center gap-1.5">
              {inputMode !== 'attempt' && (
                <div className="inline-flex p-0.5 rounded-[8px] bg-white/[0.05] shadow-[inset_0_0_0_1px_rgba(255,255,255,0.08)] flex-none"
                  role="tablist" aria-label="What the box does">
                  {(['ask', 'fix'] as const).map((k) => (
                    <button key={k} role="tab" aria-selected={tab === k} title={k === 'ask' ? 'Ask a new question about the lesson' : 'Fix the answer: add context or say "answer it", "shorter", "quotes"'}
                      onMouseDown={() => window.glass.send('overlay:focus', true)}
                      onClick={() => { setTab(k); setTabTouched(true); inputRef.current?.focus(); }}
                      className={`h-[24px] px-2 rounded-[6px] text-[11.5px] ${tab === k ? 'bg-white/[0.12] text-[#f2f4f8]' : 'text-faint hover:text-muted'}`}>
                      {k === 'ask' ? 'Ask' : 'Fix'}
                    </button>
                  ))}
                </div>
              )}
              <input ref={inputRef} className="field pr-8" value={input} placeholder={placeholder} spellCheck={false}
                onMouseDown={() => window.glass.send('overlay:focus', true)}
                onFocus={() => window.glass.send('overlay:focus', true)}
                onBlur={() => window.glass.send('overlay:focus', false)}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') submit();
                  if (e.key === 'Tab') { e.preventDefault(); setTab((t) => (t === 'ask' ? 'fix' : 'ask')); setTabTouched(true); }
                  if (e.key === 'Escape') { setInput(''); setInputMode('context'); (e.target as HTMLInputElement).blur(); }
                }} aria-label="Add context or ask" />
              <CornerDownLeft size={12} className="absolute right-2.5 top-[9px] text-faint" />
            </div>
            <div className="mt-1.5 flex items-center gap-0.5">
              <Action icon={<Lightbulb size={12} />} label="Explain" onClick={() => { setView('auto'); cmd('explain').catch(flash); }} disabled={!s.session} />
              <Action icon={<History size={12} />} label="Catch up" onClick={() => { setView('auto'); cmd('catchup', { minutes: 3 }).catch(flash); }} disabled={!s.session} />
              <Action icon={<ScanText size={12} />} label="Snip" onClick={() => window.glass.send('snip:start')} />
              {s.task && body !== 'task' && <Action icon={<ListTodo size={12} />} label="Task" onClick={() => setView('task')} />}
              {body === 'task' && <Action icon={<ListTodo size={12} />} label="Back" onClick={() => setView('auto')} />}
              <span className="ml-auto" />
              {s.session ? (
                <button className="btn btn-ghost btn-icon btn-danger" title="End lesson" onClick={() => cmd('session.stop').catch(flash)}><Square size={11} /></button>
              ) : (
                <button className="btn btn-ghost btn-icon" title="Notes" onClick={() => window.glass.send('dashboard:open', 'notes')}><NotebookPen size={12} /></button>
              )}
            </div>
            {hint && <div className="text-[11px] text-unsure mt-1 px-1">{hint}</div>}
          </div>
        )}
      </motion.div>
    </div>
  );
}

function Action({ icon, label, onClick, disabled }: { icon: React.ReactNode; label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button className="btn btn-ghost h-[24px] px-2 text-[11.5px] gap-1 whitespace-nowrap" onClick={onClick} disabled={disabled}>{icon}{label}</button>
  );
}

function RegisterNote() {
  const r = useGlass((s) => s.registerCall);
  const [, tick] = useState(0);
  useEffect(() => { if (!r) return; const t = setTimeout(() => tick((x) => x + 1), 20000); return () => clearTimeout(t); }, [r?.t]);
  if (!r || Date.now() / 1000 - r.t > 20) return null;
  return (
    <div className="mx-4 mb-1.5 chip chip-called h-auto py-1 whitespace-normal leading-snug" role="status">
      The teacher is taking the register and called your name — say “here”.
    </div>
  );
}

function GlassAsks({ onError }: { onError: (e: Error) => void }) {
  const asks = useGlass((s) => s.asks);
  const subjectId = useGlass((s) => s.lesson.subject_id);
  const [note, setNote] = useState('');
  if (!asks.length && !note) return null;
  const a = asks[asks.length - 1];
  async function act(o: { action: string; value: string }) {
    try {
      if (o.action === 'subject') await cmd('session.subject', { subject_id: o.value });
      if (o.action === 'add_text') { const msg = await addStudyText(o.value || subjectId); if (msg) setNote(msg); }
      dismissAsk(a.id);
    } catch (e) { onError(e as Error); }
  }
  return (
    <div className="mx-3 mb-2 rounded-[9px] px-3 py-2 bg-[rgba(168,180,255,0.08)] shadow-[inset_0_0_0_1px_rgba(168,180,255,0.22)]">
      {a ? (
        <>
          <div className="flex items-start gap-2">
            <span className="eyebrow text-lumen mt-[1px]">GLASS asks</span>
            <button className="ml-auto text-faint hover:text-muted text-[12px] leading-none" aria-label="Dismiss" onClick={() => dismissAsk(a.id)}>×</button>
          </div>
          <div className="text-[12.5px] mt-0.5 leading-snug">{a.question}</div>
          <div className="flex flex-wrap gap-1 mt-1.5">
            {a.options.map((o) => <button key={o.label} className="btn h-[24px] text-[11.5px]" onClick={() => act(o)}>{o.label}</button>)}
          </div>
        </>
      ) : <div className="text-[12px] text-exact leading-snug">{note}</div>}
    </div>
  );
}

function ThermalNotice() {
  const th = useGlass((s) => s.status?.thermal);
  if (!th || th.level === 'ok') return null;
  const tone = th.level === 'warm' ? 'text-muted' : th.level === 'hot' ? 'text-called' : 'text-unsure';
  return (
    <div className={`mx-4 mb-1.5 flex gap-1.5 text-[11.5px] leading-snug ${tone}`} role="status">
      <Thermometer size={13} className="mt-[1px] flex-none" />
      <span>{th.message}{th.reason ? ` (${th.reason})` : ''}</span>
    </div>
  );
}

function StatusBody({ text, detail }: { text: string; detail?: string }) {
  const s = useGlass();
  const llm = s.status?.llm;
  return (
    <div className="px-4 pb-3 pt-1">
      <div className="text-[14px] font-semibold">{text}</div>
      {detail && <div className="text-[12px] text-muted mt-0.5 leading-snug">{detail}</div>}
      {llm?.state === 'missing' && (
        <button className="btn btn-primary mt-2" onClick={() => cmd('models.pull', { model: llm.model })}>Download {llm.model} (≈2.5 GB)</button>
      )}
      {s.proc.state === 'missing' && (
        <div className="flex gap-1.5 mt-2">
          <button className="btn btn-primary" onClick={() => window.glass.invoke('engine:setup')}>Set up engine</button>
          <button className="btn" onClick={() => window.glass.invoke('engine:restart')}>I've finished — retry</button>
        </div>
      )}
      {llm?.state === 'unavailable' && (
        <button className="btn mt-2" onClick={() => cmd('llm.check')}>Retry — Ollama is running now</button>
      )}
    </div>
  );
}

function PassiveBody() {
  const s = useGlass();
  const last = s.partial?.text || s.transcript.filter((u) => u.source === 'class').slice(-1)[0]?.text || '';
  const noSpeech = s.session && !s.transcript.length && !s.partial;
  return (
    <div className="px-4 pb-2.5 pt-0.5">
      {s.lesson.topic ? (
        <>
          <div className="eyebrow mb-0.5">Now</div>
          <div className="say text-[15px] leading-snug">{s.lesson.topic}</div>
          {s.lesson.summary && <div className="text-[12px] text-muted mt-0.5 leading-snug line-clamp-2">{s.lesson.summary}</div>}
        </>
      ) : noSpeech ? (
        <div className="text-[12.5px] text-muted leading-snug">
          {s.status?.audio.state === 'running' ? 'Listening — no speech yet. Make sure the lesson is playing through this PC.' :
            s.status?.audio.state === 'error' ? `Audio capture problem: ${s.status.audio.message}` : 'Starting audio capture…'}
        </div>
      ) : (
        <div className="text-[12.5px] text-muted">Working out the topic…</div>
      )}
      {last && (
        <div className={`mt-1.5 text-[11.5px] leading-snug line-clamp-2 ${s.partial ? 'text-[#c7ccd8]' : 'text-faint'}`}>
          <span className="text-faint">Teacher: </span>{last}
        </div>
      )}
    </div>
  );
}

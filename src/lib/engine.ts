// WebSocket client for the local engine + the shared Zustand store.
import { create } from 'zustand';
import type { Answer, GlassAsk, EngineStatus, Lesson, Metrics, Mode, Notes, ScreenObs, ScreenQuestions, Session, Settings, Task, Utter } from './types';

interface EngineProc { state: string; message?: string; port?: number; token?: string }

export interface GlassState {
  proc: EngineProc;
  connected: boolean;
  status: EngineStatus | null;
  session: Session | null;
  lastEnded: string | null;
  mode: Mode;
  lesson: Lesson;
  task: Task | null;
  taskHistory: Task[];
  answers: Record<string, Answer>;
  order: string[];
  transcript: Utter[];
  partial: Utter | null;
  speaking: boolean;
  screen: ScreenObs | null;
  notes: Notes | null;
  notesMd: string;
  settings: Settings | null;
  metrics: Metrics;
  attention: { t: number; confidence: number; level: string; text: string } | null;
  waitingFor: string | null; // answer id the teacher is waiting on
  focusAnswer: string | null; // answer shown in the overlay
  asks: GlassAsk[]; // questions GLASS is asking the student
  registerCall: { t: number; text: string } | null;
  screenQuestions: ScreenQuestions | null; // questions GLASS noticed on the shared screen
  finalisedAt: number; // bumps when an ended lesson's final notes are saved
}

const initial: GlassState = {
  proc: { state: 'starting' }, connected: false, status: null, session: null, lastEnded: null, mode: 'idle',
  lesson: { subject_id: null, topic: '', subtopic: '', summary: '' }, task: null, taskHistory: [], answers: {}, order: [],
  transcript: [], partial: null, speaking: false, screen: null, notes: null, notesMd: '', settings: null, metrics: {},
  attention: null, waitingFor: null, focusAnswer: null, asks: [], registerCall: null, screenQuestions: null, finalisedAt: 0,
};

// what a lesson shows; cleared when a lesson ends and when the next one starts
const lessonBlank = (): Partial<GlassState> => ({
  lesson: { subject_id: null, topic: '', subtopic: '', summary: '' }, task: null, taskHistory: [], answers: {}, order: [],
  transcript: [], partial: null, speaking: false, screen: null, attention: null, waitingFor: null, focusAnswer: null,
  asks: [], registerCall: null, screenQuestions: null,
});

export const useGlass = create<GlassState>(() => initial);
const set = useGlass.setState;
const get = useGlass.getState;

let ws: WebSocket | null = null;
let rid = 0;
const pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void }>();
let retry: ReturnType<typeof setTimeout> | null = null;

export function cmd<T = any>(name: string, payload: Record<string, unknown> = {}): Promise<T> {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return reject(new Error('GLASS engine is not connected'));
    const id = ++rid;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ cmd: name, rid: id, ...payload }));
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(`${name} timed out`)); }
    }, 120000);
  });
}

function upsertAnswer(a: Answer) {
  const s = get();
  const exists = !!s.answers[a.id];
  const answers = { ...s.answers, [a.id]: a };
  let order = exists ? s.order : [...s.order, a.id];
  if (order.length > 60) {
    const drop = order.slice(0, order.length - 60);
    order = order.slice(-60);
    drop.forEach((k) => delete answers[k]);
  }
  // the overlay follows the newest meaningful card
  let focusAnswer = s.focusAnswer;
  if (!exists && !a.rhetorical) focusAnswer = a.id;
  set({ answers, order, focusAnswer, waitingFor: a.waiting ? a.id : s.waitingFor === a.id && !a.waiting ? null : s.waitingFor });
}

function onEvent(e: any) {
  switch (e.type) {
    case 'hello': {
      const snap = e.snapshot;
      const answers: Record<string, Answer> = {};
      (snap.answers || []).forEach((a: Answer) => (answers[a.id] = a));
      set({
        status: snap.status, session: snap.session, mode: snap.mode, lesson: snap.lesson, task: snap.task,
        taskHistory: snap.task_history || [], answers, order: (snap.answers || []).map((a: Answer) => a.id),
        transcript: snap.transcript || [], notes: snap.notes, notesMd: snap.notes_md, settings: snap.settings,
        metrics: snap.metrics || {},
      });
      break;
    }
    case 'status': set({ status: e.status }); break;
    case 'session.update': set({ session: e.session, ...(e.ended ? { lastEnded: e.ended } : {}) }); break;
    case 'mode': set({ mode: e.mode }); break;
    case 'lesson.state': set({ lesson: e.state }); break;
    case 'lesson.reset': set({ ...lessonBlank(), ...(e.ended ? {} : { notes: null, notesMd: '' }) }); break;
    case 'screen.questions': set({ screenQuestions: { id: e.id, t: e.t, questions: e.questions, new: e.new } }); break;
    case 'session.finalised': set({ finalisedAt: Date.now() }); break;
    case 'answer.update': upsertAnswer(e.answer); break;
    case 'question.waiting': set({ waitingFor: e.answer_id }); break;
    case 'attention': set({ attention: e }); break;
    case 'glass.ask': set({ asks: [...get().asks.filter((a) => a.id !== e.id), e].slice(-3) }); break;
    case 'register': set({ registerCall: { t: e.t, text: e.text } }); break;
    case 'task.update': set({ task: e.task, taskHistory: e.history || [] }); break;
    case 'speech.activity': if (e.source === 'class') set({ speaking: e.speaking }); break;
    case 'transcript.partial': set({ partial: { id: e.id, source: e.source, text: e.text, t0: e.t0, partial: true } }); break;
    case 'transcript.final': {
      const tr = [...get().transcript, { id: e.id, source: e.source, text: e.text, t0: e.t0, t1: e.t1, noise: e.noise }].slice(-300);
      const p = get().partial;
      set({ transcript: tr, partial: p && p.id === e.id ? null : p });
      break;
    }
    case 'screen.observation': set({ screen: e }); break;
    case 'notes.update': {
      // the final notes pass of a lesson that already ended must not overwrite the live one
      const live = get().session;
      if (!live || !e.session_id || e.session_id === live.id) set({ notes: e.notes, notesMd: e.markdown });
      else set({ finalisedAt: Date.now() });
      break;
    }
    case 'settings': set({ settings: e.settings }); break;
    case 'metrics': set({ metrics: e.metrics }); break;
    case 'reply': {
      const p = pending.get(e.rid);
      if (p) { pending.delete(e.rid); e.ok ? p.resolve(e.result) : p.reject(new Error(e.error)); }
      break;
    }
  }
}

function open(proc: EngineProc) {
  if (!proc.port || !proc.token) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const sock = new WebSocket(`ws://127.0.0.1:${proc.port}/?token=${proc.token}`);
  ws = sock;
  sock.onopen = () => set({ connected: true });
  sock.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (err) { console.error(err); } };
  sock.onclose = () => {
    if (ws === sock) ws = null;
    set({ connected: false });
    pending.forEach((p) => p.reject(new Error('engine disconnected')));
    pending.clear();
    if (retry) clearTimeout(retry);
    retry = setTimeout(() => open(get().proc), 1500);
  };
}

export async function connectEngine() {
  const proc = await window.glass.invoke<EngineProc>('engine:config');
  set({ proc });
  if (proc.state === 'ready') open(proc);
  window.glass.on('engine:state', (p: EngineProc) => {
    set({ proc: p });
    if (p.state === 'ready') open(p);
  });
}

// ------------------------------------------------------------------ helpers
export const IS_MAC = typeof window !== 'undefined' && window.glass?.platform === 'darwin';

/** "Control+Alt+S" -> "Ctrl+Alt+S" on Windows, "⌃⌥S" on macOS. */
export function prettyAccel(a: string) {
  if (!a) return 'Not set';
  if (!IS_MAC) return a.replace(/Control/g, 'Ctrl');
  return a.replace(/Control\+?/g, '⌃').replace(/Alt\+?/g, '⌥').replace(/Shift\+?/g, '⇧')
    .replace(/(Command|Super|Meta)\+?/g, '⌘').replace(/Space/g, ' Space');
}
export const subjectName = (id: string | null | undefined, settings: Settings | null) =>
  settings?.subjects.find((s) => s.id === id)?.name ?? (id ? id : '');

export const dismissAsk = (id: string) => set({ asks: get().asks.filter((a) => a.id !== id) });

/** Open a file picker and add a book / notes file to the student's texts. */
export async function addStudyText(subjectId: string | null): Promise<string> {
  const path = await window.glass.invoke<string | null>('texts:pick');
  if (!path) return '';
  const info = await cmd<{ title: string; chunks: number }>('texts.add', { path, subject_id: subjectId });
  return `Added “${info.title}” (${info.chunks} passages). Quotes will now come from it.`;
}

export function fmtClock(t: number) {
  return new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function fmtDate(t: number) {
  return new Date(t * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
}

/** Human description of whether a feature actually works right now. */
export function readiness(s: GlassState) {
  const st = s.status;
  if (s.proc.state === 'missing') return { ok: false, text: 'Engine not installed', detail: s.proc.message };
  if (!s.connected) return { ok: false, text: s.proc.state === 'restarting' ? 'Engine restarting…' : 'Starting engine…' };
  if (!st) return { ok: false, text: 'Connecting…' };
  if (st.llm.state === 'unavailable') return { ok: false, text: 'Ollama not running', detail: st.llm.message };
  if (st.llm.state === 'missing') return { ok: false, text: 'Language model not downloaded', detail: st.llm.message };
  if (st.llm.state === 'downloading') return { ok: false, text: `Downloading model ${Math.round((st.llm.progress || 0) * 100)}%` };
  if (st.asr.state === 'loading' || st.asr.state === 'not_loaded') return { ok: false, text: 'Loading speech model…' };
  if (st.asr.state === 'error') return { ok: false, text: 'Speech recognition unavailable', detail: st.asr.message };
  if (st.llm.state === 'loading' || st.llm.state === 'checking') return { ok: false, text: 'Warming up the AI…' };
  if (st.llm.state === 'error') return { ok: false, text: 'Model error', detail: st.llm.message };
  return { ok: true, text: 'Ready' };
}

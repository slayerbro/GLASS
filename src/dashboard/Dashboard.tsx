import { useEffect, useState } from 'react';
import { Activity, BookMarked, GraduationCap, ListTodo, Radio, Settings as Cog } from 'lucide-react';
import { cmd, connectEngine, readiness, subjectName, useGlass } from '../lib/engine';
import { LiveView } from './views/Live';
import { NotesView } from './views/Notes';
import { TasksView } from './views/Tasks';
import { SubjectsView } from './views/Subjects';
import { SettingsView } from './views/Settings';

export type ViewId = 'live' | 'notes' | 'tasks' | 'subjects' | 'settings';
const NAV: { id: ViewId; label: string; icon: React.ReactNode }[] = [
  { id: 'live', label: 'Live', icon: <Radio size={15} /> },
  { id: 'notes', label: 'Notes', icon: <BookMarked size={15} /> },
  { id: 'tasks', label: 'Tasks', icon: <ListTodo size={15} /> },
  { id: 'subjects', label: 'Subjects', icon: <GraduationCap size={15} /> },
  { id: 'settings', label: 'Settings', icon: <Cog size={15} /> },
];

export function Dashboard() {
  const s = useGlass();
  const [view, setView] = useState<ViewId>(((location.hash.slice(1) as ViewId) || 'live'));
  const ready = readiness(s);

  useEffect(() => {
    connectEngine();
    return window.glass.on('dashboard:view', (v: ViewId) => setView(v));
  }, []);

  return (
    <div className="h-full flex flex-col" style={{ background: 'rgba(10,12,18,0.42)' }}>
      {/* title bar (native window controls are drawn by Windows on the right) */}
      {/* macOS draws its traffic-light buttons on the left of this bar */}
      <div className={`drag h-10 flex items-center gap-3 flex-none ${window.glass.platform === 'darwin' ? 'pl-[84px]' : 'pl-4'}`}>
        <span className="wordmark">GLASS</span>
        <span className="text-[11.5px] text-faint">Classroom copilot</span>
      </div>
      <div className="flex-1 flex min-h-0">
        <aside className="w-[220px] flex-none px-3 pb-3 flex flex-col gap-1">
          <SessionCard />
          <nav className="mt-3 flex flex-col gap-0.5" aria-label="Sections">
            {NAV.map((n) => (
              <button key={n.id} className={`nav-item ${view === n.id ? 'active' : ''}`} onClick={() => setView(n.id)} aria-current={view === n.id}>
                {n.icon}{n.label}
                {n.id === 'tasks' && s.task && <span className="ml-auto dot dot-live" />}
              </button>
            ))}
          </nav>
          <div className="mt-auto panel p-3 text-[11.5px] leading-relaxed">
            <div className="flex items-center gap-1.5 text-muted mb-1"><Activity size={12} />System</div>
            <div className={ready.ok ? 'text-exact' : 'text-called'}>{ready.text}</div>
            {s.status?.thermal && s.status.thermal.level !== 'ok' && (
              <div className={`mt-1 leading-snug ${s.status.thermal.level === 'warm' ? 'text-muted' : 'text-unsure'}`}>{s.status.thermal.message}</div>
            )}
            {s.metrics.gpu_temp != null && (
              <div className={`mono text-[10.5px] mt-1 ${(s.metrics.gpu_temp ?? 0) >= 85 ? 'text-unsure' : (s.metrics.gpu_temp ?? 0) >= 78 ? 'text-called' : 'text-faint'}`}>
                GPU {s.metrics.gpu_temp} °C{s.metrics.cpu_perf != null ? ` · CPU speed ${s.metrics.cpu_perf}%` : ''}
              </div>
            )}
            {s.metrics.vram_used_mb != null && (
              <div className="text-faint mono text-[10.5px] mt-1">
                GPU {s.metrics.gpu_util}% · VRAM {(s.metrics.vram_used_mb / 1024).toFixed(1)}/{((s.metrics.vram_total_mb || 0) / 1024).toFixed(0)} GB
              </div>
            )}
          </div>
        </aside>
        <main className="flex-1 min-w-0 pr-3 pb-3">
          <div className="h-full panel-solid overflow-hidden">
            {view === 'live' && <LiveView />}
            {view === 'notes' && <NotesView />}
            {view === 'tasks' && <TasksView />}
            {view === 'subjects' && <SubjectsView />}
            {view === 'settings' && <SettingsView />}
          </div>
        </main>
      </div>
    </div>
  );
}

function SessionCard() {
  const s = useGlass();
  const [subject, setSubject] = useState('');
  const [err, setErr] = useState('');
  const ready = readiness(s);
  const sess = s.session;
  const elapsed = useElapsed(sess?.started_at);

  async function start() {
    setErr('');
    try { await cmd('session.start', { subject_id: subject || null }); } catch (e) { setErr((e as Error).message); }
  }

  if (sess) {
    return (
      <div className="panel p-3">
        <div className="flex items-center gap-2">
          <span className={`dot ${sess.paused ? 'dot-warn' : 'dot-live'}`} />
          <span className="text-[12.5px] font-semibold">{sess.paused ? 'Paused' : 'Lesson in progress'}</span>
          <span className="ml-auto mono text-[11px] text-faint">{elapsed}</span>
        </div>
        <select className="field h-[28px] mt-2" value={s.lesson.subject_id || ''} aria-label="Subject"
          onChange={(e) => cmd('session.subject', { subject_id: e.target.value || null })}>
          <option value="">{s.lesson.subject_id ? 'Auto-detect' : 'Detecting subject…'}</option>
          {s.settings?.subjects.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
        <div className="flex gap-1.5 mt-2">
          <button className="btn flex-1" onClick={() => cmd('session.pause', { paused: !sess.paused })}>{sess.paused ? 'Resume' : 'Pause'}</button>
          <button className="btn flex-1 btn-danger" onClick={() => cmd('session.stop')}>End lesson</button>
        </div>
      </div>
    );
  }
  return (
    <div className="panel p-3">
      <div className="text-[12.5px] font-semibold">No lesson running</div>
      {!s.settings?.consent_acknowledged ? (
        <div className="text-[11.5px] text-muted mt-1 leading-snug">Confirm capture consent in Settings before the first lesson.</div>
      ) : (
        <>
          <select className="field h-[28px] mt-2" value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="Subject">
            <option value="">Detect subject automatically</option>
            {s.settings?.subjects.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
          <button className="btn btn-primary w-full mt-2" disabled={!ready.ok} onClick={start}>Start lesson</button>
        </>
      )}
      {err && <div className="text-[11px] text-unsure mt-1">{err}</div>}
      {s.lastEnded && <div className="text-[11px] text-faint mt-2">Last lesson saved to Notes.</div>}
    </div>
  );
}

function useElapsed(start?: number) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 1000); return () => clearInterval(t); }, []);
  if (!start) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - start));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export { subjectName };

import { useEffect, useState } from 'react';
import { cmd, fmtClock, fmtDate, subjectName, useGlass } from '../../lib/engine';
import type { Task } from '../../lib/types';
import { TaskCard } from '../../overlay/TaskCard';

export function TasksView() {
  const s = useGlass();
  const [all, setAll] = useState<Task[]>([]);
  const [attempt, setAttempt] = useState('');
  const [showAttempt, setShowAttempt] = useState(false);
  useEffect(() => { if (s.connected) cmd<Task[]>('tasks.list').then(setAll).catch(() => {}); }, [s.connected, s.task?.id, s.task?.status, s.taskHistory.length]);
  const tutorAnswers = s.order.map((id) => s.answers[id]).filter((a) => a?.mode === 'tutor').reverse();
  const history = all.filter((t) => t.id !== s.task?.id);

  return (
    <div className="h-full grid grid-cols-[1.2fr_1fr] min-h-0">
      <section className="min-h-0 scroll p-6 border-r border-white/[0.06]">
        <div className="eyebrow">Current task</div>
        {s.task ? (
          <div className="glass-panel mt-2" style={{ background: 'rgba(255,255,255,0.035)' }}>
            <TaskCard task={s.task} settings={s.settings} onTutor={(mode) => cmd('tutor', { mode, task: s.task })} onAttempt={() => setShowAttempt(true)} />
            {s.task.evidence && <div className="px-4 pb-3 text-[11.5px] text-faint">Heard: “{s.task.evidence}”</div>}
          </div>
        ) : (
          <div className="text-[12.5px] text-faint mt-2 leading-relaxed max-w-md">
            When the teacher sets work — “open page 45, do questions 1 to 6, you have fifteen minutes” — it shows up here with the page, questions and time allowed.
          </div>
        )}
        {s.task && showAttempt && (
          <div className="mt-3 flex gap-2">
            <input className="field" autoFocus placeholder="Type your answer or working, e.g. 'Q2: x = 5'" value={attempt} onChange={(e) => setAttempt(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && attempt.trim()) { cmd('tutor', { mode: 'check', attempt, task: s.task }); setAttempt(''); setShowAttempt(false); } }} />
            <button className="btn" onClick={() => cmd('tutor', { mode: 'solution', task: s.task })} title="Reveal the full worked solution">Full solution</button>
          </div>
        )}
        {tutorAnswers.length > 0 && (
          <div className="mt-6 space-y-3">
            <div className="eyebrow">Help so far</div>
            {tutorAnswers.slice(0, 6).map((a) => (
              <div key={a.id} className="panel p-3">
                <div className="text-[12px] font-semibold mb-1">{a.question}</div>
                <div className="text-[12.5px] text-[#dfe3ec] whitespace-pre-wrap selectable">{a.body || '…'}</div>
              </div>
            ))}
          </div>
        )}
      </section>
      <section className="min-h-0 scroll p-6">
        <div className="eyebrow mb-2">History</div>
        {!history.length && <div className="text-[12.5px] text-faint">No earlier tasks yet.</div>}
        <div className="space-y-2">
          {history.map((t) => (
            <div key={t.id} className="panel p-3">
              <div className="flex items-center gap-2">
                <span className={`chip ${t.status === 'done' ? 'chip-exact' : ''}`}>{t.status === 'done' ? 'Completed' : t.status === 'replaced' ? 'Replaced' : t.status}</span>
                <span className="text-[11px] text-faint">{subjectName(t.subject_id, s.settings)} · {fmtDate(t.created_at)} {fmtClock(t.created_at)}</span>
              </div>
              <div className="text-[13px] font-semibold mt-1">{t.description}</div>
              <div className="text-[12px] text-muted">{[t.source, t.questions && `Q ${t.questions}`, t.deadline && `due ${t.deadline}`].filter(Boolean).join(' · ')}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

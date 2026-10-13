import { useEffect, useState } from 'react';
import { BookOpen, Check, Timer } from 'lucide-react';
import type { Settings, Task } from '../lib/types';
import { cmd, subjectName } from '../lib/engine';

function useNow(active: boolean) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

export function TaskCard({ task, settings, onTutor, onAttempt }: {
  task: Task; settings: Settings | null; onTutor: (mode: string) => void; onAttempt: () => void;
}) {
  const now = useNow(!!task.timer_end);
  const left = task.timer_end ? Math.max(0, task.timer_end - now) : null;
  const mm = left != null ? `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, '0')}` : '';
  const subj = subjectName(task.subject_id, settings);
  const meta = [task.source, task.questions && `Questions ${task.questions}`].filter(Boolean).join(' · ');

  return (
    <div className="px-4 pt-3 pb-3">
      <div className="flex items-center gap-2 mb-1">
        <BookOpen size={12} className="text-lumen" />
        <span className="eyebrow">{subj ? `${subj} — ` : ''}Current task</span>
        {task.is_homework && <span className="chip">Homework</span>}
        {task.relation === 'replace' && <span className="chip chip-lumen">New task</span>}
      </div>
      <div className="font-display text-[15.5px] font-semibold leading-snug selectable" style={{ fontFamily: 'var(--font-display)' }}>
        {task.description}
      </div>
      {meta && !(task.source && task.description.toLowerCase().includes(String(task.source).toLowerCase())) &&
        <div className="text-[12px] text-muted mt-0.5">{meta}</div>}

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {task.time_minutes != null && (
          left != null ? (
            <span className={`chip mono ${left < 120 ? 'chip-called' : 'chip-lumen'}`}><Timer size={11} />{left > 0 ? `${mm} left` : 'Time is up'}</span>
          ) : (
            <>
              <span className="chip"><Timer size={11} />{task.time_minutes} min allowed</span>
              <button className="btn h-[20px] px-2 text-[11px]" onClick={() => cmd('task.timer', { id: task.id })}
                title="The teacher didn't say when the time starts — start it yourself">Start timer</button>
            </>
          )
        )}
        {task.deadline && <span className="chip">Due {task.deadline}</span>}
      </div>

      {task.first_question && <div className="mt-2 text-[12.5px]"><span className="text-faint">Start with </span><span className="font-semibold">Question {task.first_question.replace(/^q(uestion)?\s*/i, '')}</span></div>}
      {!!task.instructions?.length && (
        <ul className="mt-1.5 text-[12px] text-muted list-disc pl-4 space-y-0.5">
          {task.instructions.slice(0, 4).map((i, k) => <li key={k}>{i}</li>)}
        </ul>
      )}
      {task.extra?.map((x, k) => (
        <div key={k} className="mt-1 text-[12px] text-muted">+ {x.description}{x.questions ? ` (${x.questions})` : ''}</div>
      ))}

      <div className="mt-2.5 flex flex-wrap gap-1">
        <button className="btn" onClick={() => onTutor('steps')}>Show steps</button>
        <button className="btn" onClick={() => onTutor('hint')}>Hint</button>
        <button className="btn" onClick={onAttempt}>Check my work</button>
        <button className="btn ml-auto" onClick={() => cmd('task.complete', { id: task.id })}><Check size={12} />Done</button>
      </div>
    </div>
  );
}

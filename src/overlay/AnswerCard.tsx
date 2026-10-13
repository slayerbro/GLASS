import { motion, AnimatePresence } from 'motion/react';
import { Check, CircleAlert, Clock3, RotateCcw, Sparkles } from 'lucide-react';
import type { Answer } from '../lib/types';
import { Markdown } from '../components/Markdown';
import { cmd } from '../lib/engine';

export type Level = 'quick' | 'explain' | 'detailed';

const KIND_LABEL: Record<string, string> = {
  calculation: 'Calculation', definition: 'Definition', explanation: 'Explain', example: 'Example',
  comparison: 'Compare', opinion: 'Opinion', recall: 'Recall',
};
const TITLES: Record<string, string> = {
  explain: 'Explaining the current topic', catchup: 'What you missed', snip: 'Your selection', ask: 'You asked',
};

function Latency({ a }: { a: Answer }) {
  const ms = a.latency.say_ms ?? a.latency.total_ms ?? a.latency.first_ms;
  if (!ms || a.status !== 'done') return null;
  return <span className="mono text-[10.5px] text-faint" title="Time from question to answer">{(ms / 1000).toFixed(1)}s</span>;
}

function Thinking({ label }: { label: string }) {
  return <div className="shimmer text-[13px] font-medium py-1">{label}</div>;
}

export function AnswerCard({ a, level, setLevel }: { a: Answer; level: Level; setLevel: (l: Level) => void }) {
  const called = !!a.directed;
  const calledSure = a.directed?.level === 'high';
  const streaming = a.status === 'streaming' || a.status === 'thinking' || a.status === 'reading';
  const lowConf = a.conf === 'low' || (!!a.unsure && a.conf !== 'high');

  // long-form cards (explain / catch-up / tutor / snip)
  if (a.mode !== 'question' && a.mode !== 'ask') {
    const title = a.mode === 'tutor' ? a.question : TITLES[a.mode] || a.question;
    return (
      <div className="px-4 pt-3 pb-3">
        <div className="flex items-center gap-2 mb-1.5">
          <Sparkles size={12} className="text-lumen" />
          <span className="eyebrow">{title}</span>
          {a.verified && a.result && <span className="chip chip-exact mono" title="Computed exactly with SymPy"><Check size={11} />exact</span>}
          <span className="ml-auto"><Latency a={a} /></span>
        </div>
        {a.status === 'error' ? <ErrorLine a={a} /> :
          !a.body ? <Thinking label={a.status === 'reading' ? 'Reading your selection…' : 'Thinking…'} /> :
            <div className="max-h-[380px] scroll pr-1 -mr-1"><Markdown text={a.body} className={streaming ? 'caret' : ''} /></div>}
        {a.mode === 'snip' && a.status === 'done' && a.ocr && (
          <div className="mt-2.5 flex gap-1">
            <button className="btn" onClick={() => cmd('tutor', { mode: 'solution', focus: a.ocr }).catch(() => {})}>Show working</button>
            <button className="btn" onClick={() => cmd('tutor', { mode: 'hint', focus: a.ocr }).catch(() => {})}>Just a hint</button>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="px-4 pt-3 pb-3">
      {/* who / what / how sure */}
      <div className="flex items-center gap-1.5 flex-wrap mb-1.5">
        {called ? (
          <span className="chip chip-called font-semibold">{calledSure ? 'You were asked' : 'You may have been asked'}</span>
        ) : a.mode === 'ask' ? <span className="chip chip-lumen">Your question</span> : <span className="chip">Class question</span>}
        {a.waiting && !a.answered_by && (
          <motion.span className="chip chip-called" initial={{ opacity: 0 }} animate={{ opacity: [0.6, 1, 0.6] }}
            transition={{ duration: 1.6, repeat: 3 }}><Clock3 size={11} />Teacher is waiting</motion.span>
        )}
        {a.kind && KIND_LABEL[a.kind] && <span className="chip">{KIND_LABEL[a.kind]}</span>}
        <span className="ml-auto"><Latency a={a} /></span>
      </div>

      <div className="text-[12px] text-muted leading-snug line-clamp-2 selectable" title={a.question}>
        {a.resolved || a.question}
      </div>

      {a.clarifications.length > 0 && (
        <div className="text-[11px] text-faint mt-0.5">with your context: <span className="text-muted">“{a.clarifications.join('”, “')}”</span></div>
      )}

      {a.verified && a.result && (
        <div className="mt-2 flex items-center gap-2">
          <span className="mono text-[14px] text-exact selectable">{a.result}</span>
          <span className="chip chip-exact" title="Calculated exactly by SymPy, not by the language model"><Check size={11} />exact</span>
        </div>
      )}

      {lowConf && a.status !== 'thinking' && (
        <div className="mt-2 flex gap-1.5 text-[12px] text-unsure leading-snug">
          <CircleAlert size={13} className="mt-[2px] flex-none" />
          <span>Not fully sure{a.unsure ? ` — ${a.unsure}` : ''}. Add a few words below.</span>
        </div>
      )}

      <div className="mt-2 min-h-[24px]">
        {a.status === 'error' ? <ErrorLine a={a} /> :
          !a.say ? <Thinking label={a.status === 'thinking' ? (a.verified ? 'Wording the answer…' : 'Reading the question…') : 'Answering…'} /> : (
            <div className={`say selectable ${called ? 'say-called' : ''} ${streaming && !a.why ? 'caret' : ''}`}>{a.say}</div>
          )}
      </div>

      {a.revised && a.previous_say && (
        <div className="mt-1 text-[11px] text-faint"><RotateCcw size={10} className="inline mr-1" />Corrected after a second check (was: <s>{a.previous_say}</s>)</div>
      )}
      {a.answered_by === 'teacher' && (
        <div className="mt-1 text-[11px] text-faint">The teacher answered it: <span className="text-muted">{a.teacher_answer}</span></div>
      )}

      <AnimatePresence initial={false}>
        {level !== 'quick' && a.why && (
          <motion.div key="why" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', stiffness: 420, damping: 38 }} className="overflow-hidden">
            <p className="mt-2 text-[12.5px] leading-relaxed text-[#cfd4df] selectable">{a.why}</p>
          </motion.div>
        )}
        {level === 'detailed' && (
          <motion.div key="det" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            className="mt-2 pt-2 border-t border-white/[0.07] max-h-[300px] scroll pr-1 -mr-1">
            {a.detailed ? <Markdown text={a.detailed} className={a.status === 'streaming' ? 'caret' : ''} /> : <Thinking label="Writing the full explanation…" />}
          </motion.div>
        )}
      </AnimatePresence>

      {a.say && (
        <div className="mt-2.5 flex items-center gap-1">
          <Seg level={level} setLevel={(l) => { setLevel(l); if (l === 'detailed' && !a.detailed) cmd('answer.expand', { id: a.id }).catch(() => {}); }} />
        </div>
      )}
    </div>
  );
}

function Seg({ level, setLevel }: { level: Level; setLevel: (l: Level) => void }) {
  const opts: [Level, string][] = [['quick', 'Quick'], ['explain', 'Explain'], ['detailed', 'Detailed']];
  return (
    <div className="inline-flex p-0.5 rounded-[8px] bg-white/[0.05] shadow-[inset_0_0_0_1px_rgba(255,255,255,0.08)]" role="tablist" aria-label="Answer length">
      {opts.map(([k, label]) => (
        <button key={k} role="tab" aria-selected={level === k} onClick={() => setLevel(k)}
          className={`h-[22px] px-2.5 rounded-[6px] text-[11.5px] transition-colors ${level === k ? 'bg-white/[0.12] text-[#f2f4f8]' : 'text-faint hover:text-muted'}`}>
          {label}
        </button>
      ))}
    </div>
  );
}

function ErrorLine({ a }: { a: Answer }) {
  return (
    <div className="flex gap-1.5 text-[12px] text-unsure leading-snug">
      <CircleAlert size={13} className="mt-[2px] flex-none" />
      <span>Couldn't produce an answer: {a.error || 'unknown error'}</span>
    </div>
  );
}

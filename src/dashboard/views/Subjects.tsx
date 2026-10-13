import { useEffect, useState } from 'react';
import { BookOpen, Pin, PinOff, Trash2 } from 'lucide-react';
import { addStudyText, cmd, fmtDate, useGlass } from '../../lib/engine';
import type { StudyText } from '../../lib/types';

interface Stat { subject_id: string | null; lessons: number; last: number; topics: string | null }
interface Formula { id: string; text: string; meaning: string; pinned: number }
interface Card { id: string; front: string; back: string }

export function SubjectsView() {
  const s = useGlass();
  const [stats, setStats] = useState<Stat[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  useEffect(() => { if (s.connected) cmd<Stat[]>('subjects.stats').then(setStats).catch(() => {}); }, [s.connected]);
  const subjects = s.settings?.subjects || [];

  return (
    <div className="h-full grid grid-cols-[1fr_1.1fr] min-h-0">
      <section className="min-h-0 scroll p-6 border-r border-white/[0.06]">
        <div className="h-title mb-4">Subjects</div>
        <div className="grid grid-cols-2 gap-2">
          {subjects.map((x) => {
            const st = stats.find((t) => t.subject_id === x.id);
            const topics = [...new Set((st?.topics || '').split('||').filter(Boolean))];
            return (
              <button key={x.id} onClick={() => setSel(x.id)} className={`panel p-3 text-left hover:bg-white/[0.06] transition-colors ${sel === x.id ? 'ring-1 ring-lumen/50' : ''}`}>
                <div className="text-[13.5px] font-semibold">{x.name}</div>
                <div className="text-[11px] text-faint">{x.board || 'Exam board not set'}</div>
                <div className="text-[11.5px] text-muted mt-2">{st ? `${st.lessons} lesson${st.lessons > 1 ? 's' : ''} · last ${fmtDate(st.last)}` : 'No lessons yet'}</div>
                {topics.length > 0 && <div className="text-[11px] text-faint mt-0.5 line-clamp-2">{topics.slice(-4).join(' · ')}</div>}
              </button>
            );
          })}
        </div>
      </section>
      <section className="min-h-0 scroll p-6">{sel ? <SubjectDetail id={sel} /> : <div className="text-faint text-[12.5px]">Pick a subject to see its formulas and flashcards.</div>}</section>
    </div>
  );
}

function SubjectDetail({ id }: { id: string }) {
  const s = useGlass();
  const [formulas, setFormulas] = useState<Formula[]>([]);
  const [cards, setCards] = useState<Card[]>([]);
  const load = () => {
    cmd<Formula[]>('formulas', { subject_id: id }).then(setFormulas).catch(() => {});
    cmd<Card[]>('flashcards', { subject_id: id }).then(setCards).catch(() => {});
  };
  useEffect(load, [id]);
  const name = s.settings?.subjects.find((x) => x.id === id)?.name;
  const [texts, setTexts] = useState<StudyText[]>([]);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);
  const loadTexts = () => cmd<StudyText[]>('texts.list', { subject_id: id }).then(setTexts).catch(() => {});
  useEffect(() => { loadTexts(); setMsg(''); }, [id]);
  return (
    <div>
      <div className="h-title">{name}</div>
      <div className="eyebrow mt-5 mb-1">Books & notes</div>
      <div className="text-[12px] text-faint leading-snug mb-2">
        Add your own copies of set texts, revision guides or class notes (PDF, EPUB, Word or text). GLASS answers from them and
        only shows quotations it can find word-for-word in them. They stay on this computer.
      </div>
      <div className="space-y-1.5">
        {texts.map((t) => (
          <div key={t.id} className="panel px-3 py-2 flex items-center gap-2">
            <BookOpen size={13} className="text-lumen flex-none" />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] truncate">{t.title}</div>
              <div className="text-[11px] text-faint">{t.filename} · {t.chunks} passages · added {fmtDate(t.added_at)}</div>
            </div>
            <button className="btn btn-ghost btn-icon btn-danger" title="Remove from GLASS (your file isn't touched)"
              onClick={async () => { await cmd('texts.delete', { id: t.id }); loadTexts(); }}><Trash2 size={12} /></button>
          </div>
        ))}
      </div>
      <button className="btn btn-primary mt-2" disabled={busy}
        onClick={async () => { setBusy(true); try { const m = await addStudyText(id); if (m) setMsg(m); loadTexts(); } catch (e) { setMsg((e as Error).message); } setBusy(false); }}>
        <BookOpen size={13} />{busy ? 'Reading the file…' : 'Add book or notes'}
      </button>
      {msg && <div className="text-[12px] text-muted mt-1.5">{msg}</div>}
      <div className="eyebrow mt-5 mb-2">Formulas</div>
      {!formulas.length && <div className="text-[12.5px] text-faint">Formulas the teacher uses are collected here automatically.</div>}
      <div className="space-y-1.5">
        {formulas.map((f) => (
          <div key={f.id} className="panel px-3 py-2 flex items-center gap-2">
            <span className="mono text-lumen text-[13px]">{f.text}</span>
            <span className="text-[12px] text-faint flex-1 truncate">{f.meaning}</span>
            <button className="btn btn-ghost btn-icon" title={f.pinned ? 'Unpin' : 'Pin to top'}
              onClick={async () => { await cmd('formula.pin', { id: f.id, pinned: !f.pinned }); load(); }}>
              {f.pinned ? <PinOff size={12} /> : <Pin size={12} />}
            </button>
          </div>
        ))}
      </div>
      <div className="eyebrow mt-6 mb-2">Flashcards</div>
      {!cards.length && <div className="text-[12.5px] text-faint">Make flashcards from any lesson in Notes.</div>}
      <div className="space-y-1.5">
        {cards.slice(0, 60).map((c) => (
          <details key={c.id} className="panel px-3 py-2">
            <summary className="text-[12.5px] font-semibold cursor-default">{c.front}</summary>
            <div className="text-[12.5px] text-muted mt-1">{c.back}</div>
          </details>
        ))}
      </div>
    </div>
  );
}

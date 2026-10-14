import { useEffect, useMemo, useRef, useState } from 'react';
import { Download, FileText, Layers, Pencil, RefreshCw, Search, Trash2 } from 'lucide-react';
import { cmd, fmtClock, fmtDate, subjectName, useGlass } from '../../lib/engine';
import { Markdown } from '../../components/Markdown';

interface SessionRow { id: string; subject_id: string | null; title: string; topic: string; started_at: number; ended_at: number | null }
interface Card { id: string; front: string; back: string }

export function NotesView() {
  const s = useGlass();
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<{ session_id: string; title: string; snippet: string }[] | null>(null);

  const load = () => cmd<SessionRow[]>('sessions.list').then(setSessions).catch(() => {});
  useEffect(() => { if (s.connected) load(); }, [s.connected, s.session?.id, s.lastEnded, s.finalisedAt]);
  useEffect(() => { if (!sel && sessions.length) setSel(s.session?.id || sessions[0].id); }, [sessions]);
  useEffect(() => {
    if (!q.trim()) { setHits(null); return; }
    const t = setTimeout(() => cmd('search', { query: q }).then(setHits).catch(() => {}), 200);
    return () => clearTimeout(t);
  }, [q]);

  const groups = useMemo(() => {
    const g: Record<string, SessionRow[]> = {};
    sessions.forEach((x) => { (g[x.subject_id || ''] ||= []).push(x); });
    return Object.entries(g);
  }, [sessions]);

  return (
    <div className="h-full grid grid-cols-[260px_1fr] min-h-0">
      <aside className="border-r border-white/[0.06] min-h-0 flex flex-col">
        <div className="p-3 border-b border-white/[0.06]">
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-[9px] text-faint" />
            <input className="field pl-8" placeholder="Search all notes" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        </div>
        <div className="flex-1 min-h-0 scroll p-2">
          {hits ? (
            hits.length ? hits.map((h) => (
              <button key={h.session_id} className="nav-item h-auto py-2 flex-col items-start w-full" onClick={() => setSel(h.session_id)}>
                <span className="text-[12.5px] text-[#e5e8ef]">{h.title}</span>
                <span className="text-[11.5px] text-faint text-left">{h.snippet}</span>
              </button>
            )) : <div className="text-[12px] text-faint p-3">No notes mention “{q}”.</div>
          ) : !sessions.length ? (
            <div className="text-[12px] text-faint p-3 leading-relaxed">Notes appear here automatically once you run a lesson.</div>
          ) : groups.map(([sid, rows]) => (
            <div key={sid} className="mb-3">
              <div className="eyebrow px-3 py-1">{subjectName(sid, s.settings) || 'Unsorted'}</div>
              {rows.map((r) => (
                <button key={r.id} className={`nav-item w-full h-auto py-1.5 flex-col items-start gap-0 ${sel === r.id ? 'active' : ''}`} onClick={() => setSel(r.id)}>
                  <span className="text-[12.5px] truncate w-full text-left">{r.topic || r.title || 'Lesson'}</span>
                  <span className="text-[11px] text-faint">{fmtDate(r.started_at)} · {fmtClock(r.started_at)}{s.session?.id === r.id ? ' · live' : ''}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      </aside>
      {sel ? <SessionNotes key={sel} sid={sel} onDeleted={() => { setSel(null); load(); }} /> :
        <div className="grid place-items-center text-faint text-[13px]">Select a lesson</div>}
    </div>
  );
}

function SessionNotes({ sid, onDeleted }: { sid: string; onDeleted: () => void }) {
  const s = useGlass();
  const live = s.session?.id === sid;
  const [detail, setDetail] = useState<any>(null);
  const [tab, setTab] = useState<'notes' | 'questions' | 'transcript' | 'cards'>('notes');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [cards, setCards] = useState<Card[]>([]);
  const [busy, setBusy] = useState('');
  const mdRef = useRef<HTMLDivElement>(null);

  const load = () => cmd('session.detail', { session_id: sid }).then((d) => { setDetail(d); setCards(d.flashcards || []); }).catch(() => {});
  useEffect(() => { load(); }, [sid, s.finalisedAt]);  // the final notes of a just-ended lesson arrive later
  useEffect(() => { if (live) { const t = setTimeout(load, 400); return () => clearTimeout(t); } }, [live, s.notesMd]);

  const md: string = live && !detail?.notes?.edited ? s.notesMd : detail?.notes?.markdown || '';
  const sess = detail?.session;
  const name = `${sess?.topic || sess?.title || 'Lesson'} ${sess ? new Date(sess.started_at * 1000).toISOString().slice(0, 10) : ''}`.trim();

  async function exportPdf() {
    const body = mdRef.current?.innerHTML || '';
    const html = `<!doctype html><html><head><meta charset="utf-8"><style>
      body{font-family:"Segoe UI Variable Text","Segoe UI",sans-serif;color:#111;max-width:720px;margin:24px auto;font-size:12.5px;line-height:1.55}
      h1{font-size:22px}h2{font-size:15px;border-bottom:1px solid #ddd;padding-bottom:3px;margin-top:22px}h3{font-size:13px}
      code{font-family:Consolas,monospace;background:#f2f2f5;padding:1px 4px;border-radius:4px} em{color:#666}
      .katex-mathml{display:none}</style></head><body>${body}</body></html>`;
    setBusy('pdf');
    await window.glass.invoke('export:pdf', { name, html }).catch(() => {});
    setBusy('');
  }

  if (!detail) return <div className="p-6 text-faint">Loading…</div>;
  return (
    <div className="min-h-0 flex flex-col">
      <div className="px-6 pt-5 pb-3 border-b border-white/[0.06]">
        <div className="eyebrow">{subjectName(sess?.subject_id, s.settings) || 'Lesson'} · {sess && fmtDate(sess.started_at)}{live ? ' · live, updating' : ''}</div>
        <div className="flex items-center gap-2 mt-0.5">
          <div className="h-title truncate">{sess?.topic || sess?.title || 'Lesson notes'}</div>
          <div className="ml-auto flex gap-1">
            {live && <button className="btn" title="Fold the latest speech into the notes now" disabled={!!busy}
              onClick={async () => { setBusy('refresh'); await cmd('notes.refresh').catch(() => {}); setBusy(''); }}>
              <RefreshCw size={12} className={busy === 'refresh' ? 'animate-spin' : ''} />Update now</button>}
            <button className="btn" onClick={() => { setDraft(md); setEditing(!editing); setTab('notes'); }}><Pencil size={12} />{editing ? 'Cancel' : 'Edit'}</button>
            <button className="btn" onClick={() => window.glass.invoke('export:markdown', { name, markdown: md })}><FileText size={12} />Markdown</button>
            <button className="btn" disabled={busy === 'pdf'} onClick={exportPdf}><Download size={12} />PDF</button>
            <button className="btn btn-icon btn-danger" title="Delete this lesson" disabled={live}
              onClick={async () => { if (confirm('Delete this lesson and its notes permanently?')) { await cmd('session.delete', { session_id: sid }); onDeleted(); } }}>
              <Trash2 size={12} /></button>
          </div>
        </div>
        <div className="flex gap-1 mt-3" role="tablist">
          {(['notes', 'questions', 'transcript', 'cards'] as const).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={`btn h-[26px] ${tab === t ? 'text-[#f2f4f8] bg-white/[0.1]' : 'btn-ghost'}`} onClick={() => setTab(t)}>
              {t === 'notes' ? 'Notes' : t === 'questions' ? `Questions (${detail.questions.length})` : t === 'transcript' ? 'Transcript' : `Flashcards${cards.length ? ` (${cards.length})` : ''}`}
            </button>
          ))}
        </div>
      </div>
      <div className="flex-1 min-h-0 scroll px-6 py-4">
        {tab === 'notes' && (editing ? (
          <div className="h-full flex flex-col gap-2">
            <textarea className="field flex-1 min-h-[360px] p-3 mono text-[12.5px] leading-relaxed" value={draft} onChange={(e) => setDraft(e.target.value)} />
            <div className="flex gap-2"><button className="btn btn-primary" onClick={async () => { await cmd('notes.save', { session_id: sid, markdown: draft }); setEditing(false); load(); }}>Save notes</button>
              <span className="text-[11.5px] text-faint self-center">Edited notes stop updating automatically for this lesson.</span></div>
          </div>
        ) : md.trim() ? (
          <div ref={mdRef} className="max-w-[760px]"><Markdown text={md} /></div>
        ) : (
          <div className="text-[12.5px] text-faint leading-relaxed max-w-md">{live ? 'Notes build up as the teacher talks — the first update arrives after a minute or two of explanation. Use “Update now” to fold in what has been said so far.' : 'No notes were produced for this lesson (it may have had no speech).'}</div>
        ))}
        {tab === 'questions' && (
          <div className="space-y-3 max-w-[760px]">
            {!detail.questions.length && <div className="text-faint text-[12.5px]">No questions were detected in this lesson.</div>}
            {detail.questions.map((x: any) => (
              <div key={x.id} className="panel p-3">
                <div className="text-[11px] text-faint">{fmtClock(x.t)} · {x.kind}{x.directed > 0.45 ? ' · asked to you' : ''}</div>
                <div className="text-[13px] font-semibold mt-0.5">{x.question}</div>
                <div className="text-[12.5px] text-[#dfe3ec] mt-1">{x.answer?.say}</div>
                {x.teacher_answer && <div className="text-[12px] text-muted mt-1">Teacher: {x.teacher_answer}</div>}
              </div>
            ))}
          </div>
        )}
        {tab === 'transcript' && (
          <div className="space-y-1.5 max-w-[760px] selectable">
            {!detail.transcript.length && <div className="text-faint text-[12.5px]">No transcript stored{s.settings?.store_transcript === false ? ' (transcript storage is off in Settings)' : ''}.</div>}
            {detail.transcript.map((u: any) => (
              <div key={u.id} className="text-[12.5px]"><span className="mono text-[10.5px] text-faint mr-2">{fmtClock(u.t0)}</span>{u.source === 'me' && <b className="text-muted mr-1">You:</b>}{u.text}</div>
            ))}
          </div>
        )}
        {tab === 'cards' && (
          <div>
            <button className="btn btn-primary mb-4" disabled={busy === 'cards' || !md.trim()}
              onClick={async () => { setBusy('cards'); try { const c = await cmd<Card[]>('flashcards.generate', { session_id: sid }); setCards([...cards, ...c]); } catch {} setBusy(''); }}>
              <Layers size={13} />{busy === 'cards' ? 'Making flashcards…' : 'Make flashcards from these notes'}
            </button>
            <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
              {cards.map((c) => <Flashcard key={c.id} c={c} />)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Flashcard({ c }: { c: Card }) {
  const [flip, setFlip] = useState(false);
  return (
    <button className="panel p-4 text-left min-h-[120px] hover:bg-white/[0.06] transition-colors" onClick={() => setFlip(!flip)}>
      <div className="eyebrow mb-1.5">{flip ? 'Answer' : 'Question'}</div>
      <div className={`text-[13px] leading-snug ${flip ? 'text-[#dfe3ec]' : 'font-semibold'}`}>{flip ? c.back : c.front}</div>
    </button>
  );
}

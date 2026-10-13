import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../styles.css';

type Rect = { x: number; y: number; width: number; height: number };

function Snip() {
  const [start, setStart] = useState<{ x: number; y: number } | null>(null);
  const [rect, setRect] = useState<Rect | null>(null);
  const [vision, setVision] = useState(false);
  const [frozen, setFrozen] = useState<string | null>(null);
  useEffect(() => window.glass.on('snip:image', (img: string) => setFrozen(img)), []);
  const visionRef = useRef(false);
  visionRef.current = vision;
  // load the vision model while the student is still dragging
  useEffect(() => { if (vision) window.glass.send('snip:vision'); }, [vision]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') window.glass.send('snip:cancel');
      if (e.key.toLowerCase() === 'v') setVision((v) => !v);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const norm = (a: { x: number; y: number }, b: { x: number; y: number }): Rect => ({
    x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y),
  });

  return (
    <div
      className="fixed inset-0"
      onMouseDown={(e) => { if (e.button === 0) { setStart({ x: e.clientX, y: e.clientY }); setRect(null); } else window.glass.send('snip:cancel'); }}
      onMouseMove={(e) => start && setRect(norm(start, { x: e.clientX, y: e.clientY }))}
      onMouseUp={() => {
        if (rect && rect.width > 12 && rect.height > 12) window.glass.send('snip:done', { ...rect, vision: visionRef.current });
        setStart(null);
      }}
    >
      {/* the frozen screenshot (what was on screen when Snip was opened) */}
      {frozen && <img src={frozen} alt="" draggable={false} className="absolute inset-0 w-full h-full select-none pointer-events-none" />}
      {/* dim everything except the selection */}
      <svg className="absolute inset-0 w-full h-full pointer-events-none">
        <defs>
          <mask id="m">
            <rect width="100%" height="100%" fill="white" />
            {rect && <rect x={rect.x} y={rect.y} width={rect.width} height={rect.height} rx="6" fill="black" />}
          </mask>
        </defs>
        <rect width="100%" height="100%" fill="rgba(8,10,16,0.45)" mask="url(#m)" />
        {rect && <rect x={rect.x} y={rect.y} width={rect.width} height={rect.height} rx="6" fill="none" stroke="#a8b4ff" strokeWidth="1.5" />}
      </svg>
      <div className="absolute left-1/2 top-6 -translate-x-1/2 glass-panel px-4 py-2 flex items-center gap-3 text-[12.5px]" style={{ background: 'rgba(14,16,22,0.85)' }}>
        <span className="wordmark">GLASS</span>
        <span className="text-muted">Drag around the question to solve it</span>
        <button className="btn h-[22px] text-[11.5px]" onMouseDown={(e) => e.stopPropagation()} onClick={() => setVision((v) => !v)}
          aria-pressed={vision} title="Use the vision model for diagrams, graphs or handwriting (slower)">
          {vision ? '◉' : '○'} Diagram / handwriting <span className="kbd">V</span>
        </button>
        <span className="kbd">Esc</span>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<Snip />);

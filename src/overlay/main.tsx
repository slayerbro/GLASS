import { createRoot } from 'react-dom/client';
import '../styles.css';
import { Overlay } from './Overlay';

window.glass.invoke('ui:get').then((ui: any) => {
  if (ui?.glass === 'tint') document.body.classList.add('glass-tint');
});
createRoot(document.getElementById('root')!).render(<Overlay />);

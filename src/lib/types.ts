// Event and data shapes shared with the Python engine (glass/*.py).

export type Mode = 'idle' | 'passive' | 'question' | 'direct' | 'task' | 'confusion';

export interface ServiceStatus {
  state: string;
  model?: string;
  device?: string;
  message?: string;
  progress?: number | null;
  [k: string]: unknown;
}

export interface EngineStatus {
  audio: ServiceStatus;
  mic: ServiceStatus;
  asr: ServiceStatus;
  llm: ServiceStatus;
  vision: ServiceStatus;
  screen: ServiceStatus;
  thermal: { level: 'ok' | 'warm' | 'hot' | 'critical'; gpu_temp?: number | null; cpu_perf?: number | null;
             reason?: string; message?: string; driver_errors?: number };
}

export interface NameHit {
  confidence: number;
  token: string;
  position: string;
  vocative: boolean;
  level: 'high' | 'possible';
}

export interface Answer {
  id: string;
  mode: 'question' | 'ask' | 'explain' | 'catchup' | 'tutor' | 'snip';
  t: number;
  status: 'thinking' | 'reading' | 'streaming' | 'done' | 'error' | 'superseded';
  question: string;
  resolved: string;
  kind: string;
  conf: '' | 'high' | 'medium' | 'low';
  unsure: string;
  calc: string;
  result: string;
  verified: boolean;
  say: string;
  why: string;
  detailed: string;
  body: string;
  directed: NameHit | null;
  waiting: boolean;
  rhetorical: boolean;
  answered_by?: 'teacher' | 'student';
  teacher_answer?: string;
  sources: string[];
  clarifications: string[];
  latency: Record<string, number>;
  error: string;
  revised: boolean;
  previous_say?: string;
  check?: string;
  level: 'quick' | 'detailed';
  tutor_mode?: string;
  focus?: string;
  ocr?: string;
  qid?: string;
  subject_id?: string | null;
  ask?: { q: string; options: string[] };
  quotes?: { q: string; ok: boolean; source: string; texts: number }[];
  feedback?: string;
  instruction?: string;
  // snips, screen questions and "answer question 2b"
  title?: string;
  origin?: 'reference' | 'screen' | null;
  questions?: SnipQuestion[]; // what GLASS read, one entry per question / part
  parts?: (SnipQuestion & { answer: string })[]; // each question with its own answer
  powers?: string[]; // "x2"-style tokens that are probably flattened powers
  can_vision?: boolean;
  replaces?: string;
}

export interface SnipQuestion {
  label: string;
  text: string;
  stem?: string;
}

export interface ScreenQuestions {
  id: string;
  t: number;
  questions: SnipQuestion[];
  new: string[];
}

export interface GlassAsk {
  id: string;
  question: string;
  options: { label: string; action: 'subject' | 'add_text' | string; value: string }[];
  t: number;
}

export interface StudyText {
  id: string;
  subject_id: string | null;
  title: string;
  filename: string;
  chars: number;
  chunks: number;
  added_at: number;
}

export interface Task {
  id: string;
  session_id?: string;
  subject_id?: string | null;
  description: string;
  source?: string | null;
  questions?: string | null;
  first_question?: string | null;
  time_minutes?: number | null;
  timer_start?: number;
  timer_end?: number;
  deadline?: string | null;
  is_homework?: boolean;
  instructions?: string[];
  relation?: string;
  status: string;
  created_at: number;
  completed_at?: number;
  evidence?: string;
  extra?: { description: string; source?: string; questions?: string; deadline?: string }[];
}

export interface Utter {
  id: string;
  source: 'class' | 'me';
  text: string;
  t0: number;
  t1?: number;
  partial?: boolean;
  noise?: boolean; // register / chatter: shown, but kept out of the AI's context
}

export interface Lesson {
  subject_id: string | null;
  topic: string;
  subtopic: string;
  summary: string;
  topic_locked?: boolean; // the student said what the lesson is about
}

export interface Subject {
  id: string;
  name: string;
  board: string;
}

export interface Settings {
  student_name: string;
  name_aliases: string[];
  profile: 'mac' | 'cool' | 'fast' | 'balanced' | 'quality';
  language: string;
  audio_device: string | null;
  mic_enabled: boolean;
  mic_device: string | null;
  screen_enabled: boolean;
  screen_source: { kind: 'window' | 'monitor'; hwnd?: number; title?: string; monitor?: number; meet?: boolean } | null;
  screen_crop: number[] | null;
  screen_autosolve?: boolean;
  exclusion_zones: number[][];
  ocr_engine: 'windows' | 'tesseract';
  vision_model: string;
  store_raw_media: boolean;
  store_transcript: boolean;
  retention_days: number;
  subjects: Subject[];
  consent_acknowledged: boolean;
  overrides: Record<string, unknown>;
  effective: Record<string, any>;
  profiles: Record<string, Record<string, any>>;
}

export interface Session {
  id: string;
  subject_id: string | null;
  started_at: number;
  title?: string;
  paused?: boolean;
  topic?: string;
}

export interface Notes {
  subject: string;
  topic: string;
  date: string;
  objectives: string[];
  summary: string;
  definitions: { term: string; definition: string; source: string }[];
  explanations: { title: string; text: string; source: string }[];
  examples: { title: string; text: string; source: string }[];
  formulas: { formula: string; meaning: string; units: string; source: string }[];
  exam_tips: { text: string; source: string }[];
  questions: { q: string; a: string; answered_by: string }[];
  corrections: string[];
  revisit: string[];
}

export interface ScreenObs {
  text: string;
  items: Record<string, string>;
  equations: string[];
  title: string;
  t: number;
  ocr_ms: number;
}

export interface Metrics {
  cpu?: number;
  ram_used_gb?: number;
  ram_total_gb?: number;
  engine_rss_mb?: number;
  ollama_rss_mb?: number;
  gpu?: string;
  gpu_util?: number;
  vram_used_mb?: number;
  gpu_temp?: number | null;
  cpu_perf?: number | null;
  thermal?: string;
  vram_total_mb?: number;
  asr_rtf?: number;
  asr_latency_ms?: number;
  vad_ms?: number;
  llm_first_token_ms?: number;
  llm_tok_s?: number;
  ocr_ms?: number;
  answer_say_ms_avg?: number;
  audio_level?: Record<string, number>;
  audio_queue?: number;
  audio_dropped?: number;
  scheduler?: { running: string | null; queued: string[]; completed: number; preempted: number; dropped: number };
  asr_model?: string;
  asr_device?: string;
  screen?: Record<string, number>;
}

declare global {
  interface Window {
    glass: {
      platform: string;
      send: (ch: string, data?: unknown) => void;
      invoke: <T = any>(ch: string, data?: unknown) => Promise<T>;
      on: (ch: string, cb: (d: any) => void) => () => void;
    };
  }
}

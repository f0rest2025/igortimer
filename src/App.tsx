import React, { useState, useEffect, useRef, useCallback } from 'react';
import { 
  Play, Pause, Square, User, FileText, Settings, 
  Download, Plus, Clock, AlertCircle, CheckCircle2,
  MoreVertical, Edit3, Trash2, Calendar
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { format, differenceInSeconds } from 'date-fns';
import { cn } from './lib/utils';

// --- Types ---
interface Client {
  id: string;
  name: string;
  rate: number;
  lastUsed: number;
}

interface TimeSegment {
  id: string;
  clientId: string;
  task: string;
  startAt: string;
  endAt: string | null;
  status: 'work' | 'pause' | 'idle' | 'disputed';
  note: string;
}

// --- Initial Data ---
const DEFAULT_CLIENTS: Client[] = [
  { id: '1', name: 'ООО "Вектор"', rate: 2500, lastUsed: Date.now() },
  { id: '2', name: 'ИП Иванов', rate: 1800, lastUsed: Date.now() - 100000 },
  { id: '3', name: 'Личный проект', rate: 0, lastUsed: Date.now() - 500000 },
];

export default function App() {
  // --- State ---
  const [clients, setClients] = useState<Client[]>(() => {
    const saved = localStorage.getItem('tracker_clients');
    return saved ? JSON.parse(saved) : DEFAULT_CLIENTS;
  });

  const [segments, setSegments] = useState<TimeSegment[]>(() => {
    const saved = localStorage.getItem('tracker_segments');
    return saved ? JSON.parse(saved) : [];
  });

  const [activeSegmentId, setActiveSegmentId] = useState<string | null>(null);
  const [activeClientId, setActiveClientId] = useState<string | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [isIdle, setIsIdle] = useState(false);
  const [showJournal, setShowJournal] = useState(false);
  const [viewMode, setViewMode] = useState<'tracker' | 'dashboard'>('tracker');
  const [searchQuery, setSearchQuery] = useState('');
  const [showSuggestions, setShowSuggestions] = useState(false);

  // Refs for tracking
  const lastActivityRef = useRef<number>(Date.now());
  const timerRef = useRef<number | null>(null);
  const searchRef = useRef<HTMLDivElement>(null);

  // --- Helpers ---
  const getDailyClients = useCallback(() => {
    const today = new Date().toDateString();
    const todayClientIds = new Set(
      segments
        .filter(s => new Date(s.startAt).toDateString() === today)
        .map(s => s.clientId)
    );
    return clients.filter(c => todayClientIds.has(c.id));
  }, [clients, segments]);

  const filteredClients = clients.filter(c => 
    c.name.toLowerCase().includes(searchQuery.toLowerCase())
  ).sort((a, b) => {
    // Prioritize daily clients in search results
    const today = new Date().toDateString();
    const aUsedToday = segments.some(s => s.clientId === a.id && new Date(s.startAt).toDateString() === today);
    const bUsedToday = segments.some(s => s.clientId === b.id && new Date(s.startAt).toDateString() === today);
    if (aUsedToday && !bUsedToday) return -1;
    if (!aUsedToday && bUsedToday) return 1;
    return b.lastUsed - a.lastUsed;
  });

  // --- Effects ---
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (searchRef.current && !searchRef.current.contains(event.target as Node)) {
        setShowSuggestions(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  useEffect(() => {
    localStorage.setItem('tracker_clients', JSON.stringify(clients));
    localStorage.setItem('tracker_segments', JSON.stringify(segments));
  }, [clients, segments]);

  // Main Timer Tick
  useEffect(() => {
    if (activeSegmentId) {
      const activeSeg = segments.find(s => s.id === activeSegmentId);
      if (activeSeg && activeSeg.status === 'work') {
        timerRef.current = window.setInterval(() => {
          setSeconds(s => s + 1);
          checkIdle();
        }, 1000);
      }
    } else {
      setSeconds(0);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [activeSegmentId, segments]);

  // Idle Detection Simulation (Web version)
  const checkIdle = useCallback(() => {
    const now = Date.now();
    const diff = (now - lastActivityRef.current) / 1000;
    if (diff > 300 && !isIdle) { // 5 minutes threshold
      setIsIdle(true);
    }
  }, [isIdle]);

  useEffect(() => {
    const handleActivity = () => {
      lastActivityRef.current = Date.now();
      if (isIdle) setIsIdle(false);
    };
    window.addEventListener('mousemove', handleActivity);
    window.addEventListener('keydown', handleActivity);
    return () => {
      window.removeEventListener('mousemove', handleActivity);
      window.removeEventListener('keydown', handleActivity);
    };
  }, [isIdle]);

  // --- Actions ---
  const handleClientSelect = (clientId: string) => {
    setClients(prev => prev.map(c => c.id === clientId ? { ...c, lastUsed: Date.now() } : c));
    startWork(clientId);
    setSearchQuery('');
    setShowSuggestions(false);
  };

  const handleCreateAndStart = () => {
    if (!searchQuery.trim()) return;
    const newId = Math.random().toString(36).substr(2, 9);
    const newClient: Client = {
      id: newId,
      name: searchQuery,
      rate: 0,
      lastUsed: Date.now()
    };
    setClients(prev => [newClient, ...prev]);
    startWork(newId);
    setSearchQuery('');
    setShowSuggestions(false);
  };

  const startWork = (clientId: string) => {
    const newId = Math.random().toString(36).substr(2, 9);
    const newSegment: TimeSegment = {
      id: newId,
      clientId,
      task: 'Консультация',
      startAt: new Date().toISOString(),
      endAt: null,
      status: 'work',
      note: ''
    };
    
    // Close current if any
    if (activeSegmentId) {
      stopWork();
    }

    setSegments(prev => [newSegment, ...prev]);
    setActiveSegmentId(newId);
    setActiveClientId(clientId);
    setIsIdle(false);
  };

  const stopWork = () => {
    if (!activeSegmentId) return;
    
    setSegments(prev => prev.map(s => 
      s.id === activeSegmentId 
        ? { ...s, endAt: new Date().toISOString() } 
        : s
    ));
    setActiveSegmentId(null);
  };

  const togglePause = () => {
    if (!activeSegmentId) return;
    
    const activeSeg = segments.find(s => s.id === activeSegmentId);
    if (!activeSeg) return;

    if (activeSeg.status === 'work') {
      // Create pause segment
      stopWork();
      const newId = Math.random().toString(36).substr(2, 9);
      const pauseSegment: TimeSegment = {
        id: newId,
        clientId: activeSeg.clientId,
        task: 'Пауза',
        startAt: new Date().toISOString(),
        endAt: null,
        status: 'pause',
        note: ''
      };
      setSegments(prev => [pauseSegment, ...prev]);
      setActiveSegmentId(newId);
    } else {
      // Resume work
      stopWork();
      startWork(activeSeg.clientId);
    }
  };

  const formatTime = (totalSeconds: number) => {
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const activeClient = clients.find(c => c.id === activeClientId);
  const activeSegment = segments.find(s => s.id === activeSegmentId);

  return (
    <div className="min-h-screen bg-[#0f1115] text-[#e2e8f0] font-sans selection:bg-emerald-500/30">
      {/* Background Grid Pattern */}
      <div className="fixed inset-0 pointer-events-none opacity-20" 
           style={{ backgroundImage: 'radial-gradient(#334155 1px, transparent 1px)', backgroundSize: '24px 24px' }} />

      {/* --- Main Dashboard --- */}
      <main className={cn("p-8 transition-all duration-500", viewMode === 'tracker' ? "blur-md scale-[0.98] opacity-50" : "opacity-100")}>
        <div className="max-w-6xl mx-auto space-y-8">
          <header className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold tracking-tight text-white flex items-center gap-3">
                <Clock className="w-8 h-8 text-emerald-400" />
                Time Tracker Pro
              </h1>
              <p className="text-slate-400 mt-1 italic font-serif">Журнал работы консультанта</p>
            </div>
            <div className="flex gap-4">
              <button 
                onClick={() => setViewMode('tracker')}
                className="px-6 py-2 bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 rounded-lg hover:bg-emerald-500/20 transition-all font-medium"
              >
                Вернуться к таймеру
              </button>
              <button className="p-2 bg-slate-800 rounded-lg border border-slate-700 hover:bg-slate-700 transition-all">
                <Settings className="w-5 h-5" />
              </button>
            </div>
          </header>

          {/* Stats Bar */}
          <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
            <StatCard label="Сегодня отработано" value="06:45:12" icon={CheckCircle2} color="text-emerald-400" />
            <StatCard label="Простои / Паузы" value="01:12:05" icon={AlertCircle} color="text-amber-400" />
            <StatCard label="Ожидаемая выплата" value="16,800 ₽" icon={Download} color="text-blue-400" />
            <StatCard label="Активных клиентов" value={clients.length.toString()} icon={User} color="text-purple-400" />
          </div>

          {/* Journal Table */}
          <div className="bg-[#1a1d23] rounded-xl border border-slate-800 overflow-hidden shadow-2xl">
            <div className="p-6 border-bottom border-slate-800 flex items-center justify-between">
              <h2 className="text-xl font-semibold flex items-center gap-2">
                <FileText className="w-5 h-5 text-slate-400" />
                Последние сессии
              </h2>
              <div className="flex gap-3">
                <button className="text-xs uppercase tracking-widest font-bold px-3 py-1.5 border border-slate-700 rounded hover:bg-slate-800 flex items-center gap-2">
                  <Download className="w-3.5 h-3.5" /> Экспорт Excel
                </button>
                <button className="text-xs uppercase tracking-widest font-bold px-3 py-1.5 bg-emerald-600 text-white rounded hover:bg-emerald-500 flex items-center gap-2">
                  <Plus className="w-3.5 h-3.5" /> Новая запись
                </button>
              </div>
            </div>
            
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="bg-slate-900/50 text-[11px] uppercase tracking-wider font-bold text-slate-500 border-y border-slate-800">
                    <th className="px-6 py-3 font-serif italic text-slate-400">Дата</th>
                    <th className="px-6 py-3">Клиент</th>
                    <th className="px-6 py-3">Задача</th>
                    <th className="px-6 py-3">Период</th>
                    <th className="px-6 py-3">Длительность</th>
                    <th className="px-6 py-3">Статус</th>
                    <th className="px-6 py-3">Действия</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {segments.slice(0, 10).map((seg) => {
                    const client = clients.find(c => c.id === seg.clientId);
                    const start = new Date(seg.startAt);
                    const end = seg.endAt ? new Date(seg.endAt) : new Date();
                    const diff = Math.floor((end.getTime() - start.getTime()) / 1000);
                    
                    return (
                      <tr key={seg.id} className="hover:bg-slate-800/30 transition-colors group">
                        <td className="px-6 py-4 text-xs font-mono text-slate-500">{format(start, 'dd.MM')}</td>
                        <td className="px-6 py-4 font-medium text-white">{client?.name || 'N/A'}</td>
                        <td className="px-6 py-4 text-slate-400 text-sm">{seg.task}</td>
                        <td className="px-6 py-4 text-xs font-mono text-slate-500">
                          {format(start, 'HH:mm')} - {seg.endAt ? format(end, 'HH:mm') : '...'}
                        </td>
                        <td className="px-6 py-4 text-sm tabular-nums font-semibold">{formatTime(diff)}</td>
                        <td className="px-6 py-4">
                          <span className={cn(
                            "px-2 py-0.5 rounded-full text-[10px] uppercase font-bold tracking-tighter",
                            seg.status === 'work' ? "bg-emerald-500/10 text-emerald-400" :
                            seg.status === 'pause' ? "bg-amber-500/10 text-amber-400" :
                            "bg-slate-500/10 text-slate-400"
                          )}>
                            {seg.status === 'work' ? 'Работа' : seg.status === 'pause' ? 'Пауза' : 'Другое'}
                          </span>
                        </td>
                        <td className="px-6 py-4">
                          <div className="flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                            <button className="p-1 hover:text-white transition-colors"><Edit3 className="w-4 h-4" /></button>
                            <button 
                              onClick={() => setSegments(prev => prev.filter(s => s.id !== seg.id))}
                              className="p-1 hover:text-red-400 transition-colors"><Trash2 className="w-4 h-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                  {segments.length === 0 && (
                    <tr>
                      <td colSpan={7} className="px-6 py-12 text-center text-slate-500 italic">Журнал пуст. Начните работу с клиентом.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
          
          <div className="text-center">
            <p className="text-slate-600 text-xs flex items-center justify-center gap-2">
              <AlertCircle className="w-3 h-3" />
              Все данные хранятся локально в браузере. Для использования SQLite запустите Python-версию.
            </p>
          </div>
        </div>
      </main>

      {/* --- Floating Tracker Window (Simulation) --- */}
      <AnimatePresence>
        {viewMode === 'tracker' && (
          <motion.div
            drag
            dragMomentum={false}
            initial={{ opacity: 0, scale: 0.9, y: 100, x: 100 }}
            animate={{ opacity: 1, scale: 1, y: 100, x: 100 }}
            exit={{ opacity: 0, scale: 0.9 }}
            className="fixed z-50 w-72 touch-none"
            id="floating-tracker"
          >
            <div className="bg-[#181a1f]/95 backdrop-blur-xl border border-white/10 rounded-2xl shadow-[0_32px_64px_-16px_rgba(0,0,0,0.6)] overflow-hidden">
              {/* Drag Handle Top Bar */}
              <div className="h-2 bg-emerald-500/20 cursor-grab active:cursor-grabbing" />
              
              <div className="p-5 space-y-4">
                {/* Client Select / Search */}
                <div className="relative" ref={searchRef}>
                  <p className="text-[10px] uppercase tracking-widest font-black text-slate-500 mb-1.5">Клиент</p>
                  
                  {activeClientId ? (
                    <div className="flex items-center justify-between group/client">
                      <h3 className="text-sm font-bold text-white truncate drop-shadow-sm flex items-center gap-2">
                        <User className="w-3.5 h-3.5 text-emerald-400" />
                        {activeClient?.name}
                      </h3>
                      <button 
                        onClick={() => { stopWork(); setActiveClientId(null); }}
                        className="text-[9px] font-bold text-slate-500 hover:text-red-400 opacity-0 group-hover/client:opacity-100 transition-opacity"
                      >
                        СМЕНИТЬ
                      </button>
                    </div>
                  ) : (
                    <div className="relative">
                      <div className="relative flex items-center">
                        <input 
                          type="text"
                          value={searchQuery}
                          onFocus={() => setShowSuggestions(true)}
                          onChange={(e) => { setSearchQuery(e.target.value); setShowSuggestions(true); }}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' && searchQuery.trim()) handleCreateAndStart();
                          }}
                          placeholder="Поиск или новый..."
                          className="w-full bg-slate-800/50 border border-slate-700/50 rounded-xl px-3 py-2 text-xs font-medium text-white placeholder:text-slate-600 focus:outline-none focus:ring-1 focus:ring-emerald-500/50 transition-all"
                        />
                        <div className="absolute right-3 text-slate-500">
                          <User className="w-3.5 h-3.5" />
                        </div>
                      </div>

                      {/* Autocomplete Suggestions */}
                      <AnimatePresence>
                        {showSuggestions && (
                          <motion.div 
                            initial={{ opacity: 0, y: -10 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0, y: -10 }}
                            className="absolute z-60 top-full inset-x-0 mt-2 bg-[#1e2229] border border-white/10 rounded-xl shadow-2xl overflow-hidden max-h-48 overflow-y-auto custom-scrollbar"
                          >
                            {/* Priority: Used Today */}
                            {searchQuery === '' && getDailyClients().length > 0 && (
                              <div className="p-2 border-b border-white/5">
                                <p className="px-2 pb-1 text-[9px] font-black text-emerald-500/50 uppercase tracking-tighter">Сегодняшние</p>
                                {getDailyClients().map(c => (
                                  <button
                                    key={c.id}
                                    onClick={() => handleClientSelect(c.id)}
                                    className="w-full text-left px-3 py-2 text-xs font-medium text-slate-200 hover:bg-emerald-500/10 rounded-lg transition-colors flex items-center justify-between"
                                  >
                                    {c.name}
                                    <Clock className="w-3 h-3 text-emerald-500/40" />
                                  </button>
                                ))}
                              </div>
                            )}

                            {filteredClients.map(c => (
                              <button
                                key={c.id}
                                onClick={() => handleClientSelect(c.id)}
                                className="w-full text-left px-3 py-2 text-xs font-medium text-slate-200 hover:bg-white/5 transition-colors flex items-center justify-between"
                              >
                                {c.name}
                                {getDailyClients().some(dc => dc.id === c.id) && <Clock className="w-3 h-3 text-emerald-500/40" />}
                              </button>
                            ))}

                            {searchQuery && !filteredClients.some(c => c.name.toLowerCase() === searchQuery.toLowerCase()) && (
                              <button
                                onClick={handleCreateAndStart}
                                className="w-full text-left px-3 py-2 text-xs font-bold text-emerald-400 bg-emerald-500/5 hover:bg-emerald-500/10 flex items-center gap-2"
                              >
                                <Plus className="w-3.5 h-3.5" />
                                Создать "{searchQuery}"
                              </button>
                            )}
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                  )}
                </div>

                {/* Timer Display */}
                <div className="text-center py-2 relative">
                  <div className="absolute inset-0 bg-emerald-500/5 blur-2xl rounded-full" />
                  <p className="text-5xl font-black font-mono tracking-tighter text-white tabular-nums drop-shadow-[0_0_15px_rgba(16,185,129,0.3)]">
                    {formatTime(seconds)}
                  </p>
                  <p className={cn(
                    "text-[10px] sm:text-[11px] font-bold uppercase tracking-widest mt-2",
                    activeSegmentId ? "text-emerald-400" : "text-slate-500"
                  )}>
                    {activeSegmentId ? (activeSegment?.status === 'work' ? 'Учет времени' : 'Пауза') : 'Таймер остановлен'}
                  </p>
                </div>

                {/* Controls */}
                <div className="flex items-center justify-between gap-2 pt-2">
                  <div className="flex gap-2">
                    {!activeSegmentId ? (
                      <button 
                        onClick={() => activeClientId && startWork(activeClientId)}
                        disabled={!activeClientId}
                        className="w-10 h-10 flex items-center justify-center bg-emerald-500 text-black rounded-full hover:bg-emerald-400 transition-all disabled:opacity-30 disabled:grayscale"
                      >
                        <Play className="w-5 h-5 fill-current" />
                      </button>
                    ) : (
                      <>
                        <button 
                          onClick={togglePause}
                          className="w-10 h-10 flex items-center justify-center bg-white/10 text-white rounded-full hover:bg-white/20 transition-all"
                        >
                          {activeSegment?.status === 'work' ? <Pause className="w-5 h-5 fill-current" /> : <Play className="w-5 h-5 fill-current" />}
                        </button>
                        <button 
                          onClick={stopWork}
                          className="w-10 h-10 flex items-center justify-center bg-red-500/20 text-red-400 rounded-full hover:bg-red-500/30 transition-all"
                        >
                          <Square className="w-4 h-4 fill-current" />
                        </button>
                      </>
                    )}
                  </div>

                  <div className="flex gap-1">
                    <button 
                      onClick={() => setViewMode('dashboard')}
                      className="p-2.5 text-slate-400 hover:text-white hover:bg-white/5 rounded-xl transition-all"
                    >
                      <FileText className="w-5 h-5" />
                    </button>
                    <button className="p-2.5 text-slate-400 hover:text-white hover:bg-white/5 rounded-xl transition-all">
                      <MoreVertical className="w-5 h-5" />
                    </button>
                  </div>
                </div>
              </div>

              {/* Idle Overlay (Simulation) */}
              {isIdle && activeSegmentId && (
                <motion.div 
                  initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                  className="absolute inset-0 bg-slate-900/90 backdrop-blur-sm p-4 flex flex-col items-center justify-center text-center space-y-4"
                >
                  <AlertCircle className="w-8 h-8 text-amber-400" />
                  <div>
                    <p className="text-xs font-bold text-white leading-tight">Обнаружен простой!</p>
                    <p className="text-[10px] text-slate-400 mt-1">Что сделать с временем?</p>
                  </div>
                  <div className="grid grid-cols-2 gap-2 w-full">
                    <button onClick={() => setIsIdle(false)} className="text-[9px] font-bold bg-emerald-500 text-black py-1.5 rounded">ОСТАВИТЬ</button>
                    <button onClick={() => { togglePause(); setIsIdle(false); }} className="text-[9px] font-bold bg-amber-500/20 text-amber-400 py-1.5 rounded">ПАУЗА</button>
                  </div>
                </motion.div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Floating Instructions Toggle */}
      <button 
        onClick={() => setViewMode(prev => prev === 'dashboard' ? 'tracker' : 'dashboard')}
        className="fixed bottom-6 right-6 p-4 bg-emerald-600 text-white rounded-full shadow-xl hover:scale-110 active:scale-95 transition-all z-[60]"
      >
        {viewMode === 'dashboard' ? <Clock className="w-6 h-6" /> : <FileText className="w-6 h-6" />}
      </button>

      {/* Footer Branding */}
      <footer className="fixed bottom-4 left-6 text-slate-700 text-[10px] uppercase tracking-widest font-black pointer-events-none">
        Consultant Time Engine v1.0
      </footer>
    </div>
  );
}

// --- Components ---
function StatCard({ label, value, icon: Icon, color }: { label: string, value: string, icon: any, color: string }) {
  return (
    <div className="bg-[#1a1d23] p-6 rounded-xl border border-slate-800 flex flex-col gap-3 relative overflow-hidden group">
      <div className="absolute top-0 right-0 p-4 opacity-5 group-hover:opacity-10 transition-opacity">
        <Icon className="w-12 h-12" />
      </div>
      <p className="text-xs font-black uppercase tracking-widest text-slate-500">{label}</p>
      <div className="flex items-end justify-between">
        <p className={cn("text-2xl font-bold tracking-tight text-white")}>{value}</p>
        <span className={cn("p-1.5 rounded-lg bg-slate-800", color)}>
          <Icon className="w-4 h-4" />
        </span>
      </div>
    </div>
  );
}

import { useState } from 'react';
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { X, AlertCircle, Copy, Upload } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { cn } from "@/lib/utils";
import { api } from '@/lib/api';
import { ClaudeConnect } from './ClaudeConnect';

interface SettingsPanelProps {
    onClose: () => void;
}

export function SettingsPanel({ onClose }: SettingsPanelProps) {
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [logs, setLogs] = useState<string[]>([]);
    const [activeTab, setActiveTab] = useState<'data' | 'log' | 'layout'>('data');

    // --- Log tab state ---
    const nowLocal = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const [tagType, setTagType] = useState('caffeine');
    const [tagComment, setTagComment] = useState('');
    const [tagTime, setTagTime] = useState(nowLocal());
    const [woActivity, setWoActivity] = useState('walking');
    const [woIntensity, setWoIntensity] = useState('moderate');
    const [woStart, setWoStart] = useState(nowLocal());
    const [woMinutes, setWoMinutes] = useState('30');

    const handleSaveTag = async () => {
        setLoading(true); setError(null);
        try {
            await api.createTag({ tag_type_code: tagType, comment: tagComment || undefined, start_time: tagTime });
            addLog(`Tag saved: ${tagType}${tagComment ? ` (${tagComment})` : ''}`);
            setTagComment('');
        } catch (err: any) { setError(err.message); }
        finally { setLoading(false); }
    };

    const handleSaveWorkout = async () => {
        setLoading(true); setError(null);
        try {
            const start = new Date(woStart);
            const end = new Date(start.getTime() + parseInt(woMinutes || '30', 10) * 60000);
            const toLocalIso = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
            const res = await api.createWorkout({
                activity: woActivity, intensity: woIntensity,
                start_time: toLocalIso(start), end_time: toLocalIso(end),
            });
            addLog(`Workout saved: ${woActivity} ${woMinutes} min ≈ ${res.calories} kcal (${res.hr_samples} HR samples)`);
        } catch (err: any) { setError(err.message); }
        finally { setLoading(false); }
    };

    const addLog = (msg: string) => setLogs(prev => [...prev, `[${new Date().toLocaleTimeString()}] ${msg}`]);

    return (
        <div className="w-[400px] border-l bg-card flex flex-col h-full">
            {/* Header */}
            <div className="p-6 border-b flex items-center justify-between">
                <h2 className="text-lg font-semibold">Settings</h2>
                <Button variant="ghost" size="icon" onClick={onClose}>
                    <X className="h-4 w-4" />
                </Button>
            </div>

            {/* Tabs */}
            <div className="flex border-b">
                <button
                    className={cn(
                        "flex-1 py-3 text-sm font-medium border-b-2 transition-colors",
                        activeTab === 'data'
                            ? "border-primary text-primary"
                            : "border-transparent text-muted-foreground hover:text-foreground"
                    )}
                    onClick={() => setActiveTab('data')}
                >
                    Data
                </button>
                <button
                    className={cn(
                        "flex-1 py-3 text-sm font-medium border-b-2 transition-colors",
                        activeTab === 'log'
                            ? "border-primary text-primary"
                            : "border-transparent text-muted-foreground hover:text-foreground"
                    )}
                    onClick={() => setActiveTab('log')}
                >
                    Log
                </button>
                <button
                    className={cn(
                        "flex-1 py-3 text-sm font-medium border-b-2 transition-colors",
                        activeTab === 'layout'
                            ? "border-primary text-primary"
                            : "border-transparent text-muted-foreground hover:text-foreground"
                    )}
                    onClick={() => setActiveTab('layout')}
                >
                    Layout
                </button>
            </div>

            <div className="flex-1 p-6 space-y-6 overflow-y-auto">
                {activeTab === 'data' && (
                    <>
                        {/* Data source */}
                        <div className="space-y-4">
                            <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Data Source</h3>
                            <p className="text-xs text-muted-foreground">
                                Data syncs automatically from the ring over BLE (ringlink daemon).
                                Use the ring status indicator in the header to check the connection,
                                or dock the ring for ~5 s to force an instant catch.
                            </p>
                        </div>

                        {/* AI Analyst — Claude OAuth */}
                        <div className="pt-4 border-t">
                            <ClaudeConnect />
                        </div>
                    </>
                )}

                {activeTab === 'log' && (
                    <>
                        {/* Tag logger */}
                        <div className="space-y-3">
                            <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Add Tag</h3>
                            <div className="grid grid-cols-2 gap-2">
                                <select value={tagType} onChange={e => setTagType(e.target.value)}
                                    className="h-9 rounded-md border border-input bg-transparent px-3 text-sm">
                                    {['caffeine', 'alcohol', 'late_meal', 'stress', 'sick', 'nap', 'meditation', 'melatonin', 'travel', 'custom'].map(t =>
                                        <option key={t} value={t} className="bg-card">{t.replace('_', ' ')}</option>)}
                                </select>
                                <Input type="datetime-local" value={tagTime} onChange={e => setTagTime(e.target.value)} />
                            </div>
                            <Input placeholder="Comment (optional)" value={tagComment} onChange={e => setTagComment(e.target.value)} />
                            <Button className="w-full" onClick={handleSaveTag} disabled={loading}>Save Tag</Button>
                        </div>

                        {/* Workout logger */}
                        <div className="space-y-3 pt-4 border-t">
                            <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Add Workout</h3>
                            <div className="grid grid-cols-2 gap-2">
                                <select value={woActivity} onChange={e => setWoActivity(e.target.value)}
                                    className="h-9 rounded-md border border-input bg-transparent px-3 text-sm">
                                    {['walking', 'running', 'cycling', 'strength', 'hiit', 'yoga', 'swimming', 'hiking', 'sports', 'other'].map(t =>
                                        <option key={t} value={t} className="bg-card">{t}</option>)}
                                </select>
                                <select value={woIntensity} onChange={e => setWoIntensity(e.target.value)}
                                    className="h-9 rounded-md border border-input bg-transparent px-3 text-sm">
                                    {['easy', 'moderate', 'hard'].map(t => <option key={t} value={t} className="bg-card">{t}</option>)}
                                </select>
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                                <Input type="datetime-local" value={woStart} onChange={e => setWoStart(e.target.value)} />
                                <Input type="number" min="1" placeholder="minutes" value={woMinutes} onChange={e => setWoMinutes(e.target.value)} />
                            </div>
                            <Button className="w-full" onClick={handleSaveWorkout} disabled={loading}>Save Workout</Button>
                            <p className="text-[10px] text-muted-foreground">
                                Calories are estimated from your recorded heart rate over the
                                workout window (falls back to intensity if no HR data).
                            </p>
                        </div>

                        {error && (
                            <Alert variant="destructive">
                                <AlertCircle className="h-4 w-4" />
                                <AlertTitle>Error</AlertTitle>
                                <AlertDescription>{error}</AlertDescription>
                            </Alert>
                        )}

                        {logs.length > 0 && (
                            <p className="text-xs text-muted-foreground font-mono">{logs[logs.length - 1]}</p>
                        )}
                    </>
                )}

                {activeTab === 'layout' && (
                    <div className="space-y-4">
                        <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">Layout Actions</h3>

                        <div className="grid grid-cols-1 gap-3">
                            <Button variant="outline" onClick={() => {
                                api.getLayout()
                                    .then(data => {
                                        const layoutJson = JSON.stringify(data, null, 2);
                                        navigator.clipboard.writeText(layoutJson);
                                        addLog("Layout config copied to clipboard.");
                                    })
                                    .catch(err => {
                                        console.error("Failed to fetch layout", err);
                                    });
                            }}>
                                <Copy className="mr-2 h-4 w-4" />
                                Copy Layout to Clipboard
                            </Button>

                            <div className="space-y-2">
                                <Label>Import Layout</Label>
                                <textarea
                                    placeholder="Paste layout JSON here..."
                                    className="flex min-h-[150px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 font-mono text-[10px]"
                                    id="import-layout-area"
                                />
                                <Button
                                    variant="outline"
                                    className="w-full"
                                    onClick={async () => {
                                        const el = document.getElementById('import-layout-area') as HTMLTextAreaElement;
                                        if (!el || !el.value) return;

                                        try {
                                            const rawJson = JSON.parse(el.value);
                                            let payload = rawJson;

                                            // Handle case where export is wrapped in "dashboard" key
                                            if (rawJson.dashboard && rawJson.dashboard.dashboards) {
                                                payload = rawJson.dashboard;
                                            }

                                            // Validation
                                            if (!payload.dashboards && !payload.widgets) {
                                                alert("Invalid JSON: Must contain 'dashboards' or 'widgets' property.");
                                                return;
                                            }

                                            await api.saveLayout(payload);
                                            alert("Layout imported successfully! The page will reload.");
                                            window.location.reload();
                                            el.value = "";
                                        } catch (e: any) {
                                            alert("Import Failed: " + e.message);
                                        }
                                    }}
                                >
                                    <Upload className="mr-2 h-4 w-4" />
                                    Import Layout
                                </Button>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div >
    );
}

import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Sparkles, ExternalLink, Check, Loader2 } from 'lucide-react';
import { api } from '@/lib/api';

/**
 * Connect the AI analyst to Claude via subscription OAuth (Claude Pro/Max
 * account, no API key). Flow: Connect -> browser login on claude.ai ->
 * paste the code shown on the callback page -> done.
 */
export function ClaudeConnect() {
    const [connected, setConnected] = useState<boolean | null>(null);
    const [authStarted, setAuthStarted] = useState(false);
    const [code, setCode] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const refresh = () => {
        api.getClaudeStatus().then(s => setConnected(!!s.connected)).catch(() => setConnected(null));
    };
    useEffect(refresh, []);

    const startAuth = async () => {
        setBusy(true); setError(null);
        try {
            const { auth_url } = await api.startClaudeAuth();
            window.open(auth_url, '_blank');
            setAuthStarted(true);
        } catch (e: any) {
            setError(e.message);
        } finally {
            setBusy(false);
        }
    };

    const finishAuth = async () => {
        if (!code.trim()) return;
        setBusy(true); setError(null);
        try {
            await api.finishClaudeAuth(code.trim());
            setAuthStarted(false);
            setCode('');
            refresh();
        } catch (e: any) {
            setError(e.message);
        } finally {
            setBusy(false);
        }
    };

    const disconnect = async () => {
        setBusy(true);
        try { await api.logoutClaude(); } finally { setBusy(false); refresh(); }
    };

    return (
        <div className="space-y-3">
            <h3 className="font-medium text-sm text-muted-foreground uppercase tracking-wider">
                AI Analyst — Claude
            </h3>

            {connected ? (
                <div className="flex items-center justify-between rounded-md border border-border p-3">
                    <div className="flex items-center gap-2 text-sm">
                        <Check className="h-4 w-4 text-green-500" />
                        Connected with your Claude account
                    </div>
                    <Button variant="outline" size="sm" onClick={disconnect} disabled={busy}>
                        Disconnect
                    </Button>
                </div>
            ) : (
                <div className="space-y-2 rounded-md border border-border p-3">
                    <p className="text-xs text-muted-foreground">
                        Sign in with your Claude Pro/Max subscription (OAuth, no API key).
                        A browser window opens — log in, then paste the code shown back here.
                    </p>
                    <Button size="sm" onClick={startAuth} disabled={busy} className="gap-1.5">
                        {busy && !authStarted ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                        Connect Claude
                        <ExternalLink className="h-3 w-3 opacity-60" />
                    </Button>
                    {authStarted && (
                        <div className="flex gap-2 pt-1">
                            <Input
                                placeholder="Paste authorization code (code#state)"
                                value={code}
                                onChange={e => setCode(e.target.value)}
                                className="h-8 text-xs"
                            />
                            <Button size="sm" onClick={finishAuth} disabled={busy || !code.trim()}>
                                {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : 'Finish'}
                            </Button>
                        </div>
                    )}
                </div>
            )}

            {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
    );
}

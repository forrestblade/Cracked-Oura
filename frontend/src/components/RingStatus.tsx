import { useEffect, useState, useCallback } from 'react';
import { Button } from '@/components/ui/button';
import { RefreshCw, Bluetooth } from 'lucide-react';
import { api } from '@/lib/api';

export interface RingStatusData {
    available: boolean;
    indicator: 'ok' | 'syncing' | 'waiting' | 'stale' | 'error';
    syncing: boolean;
    waiting?: boolean;
    live?: boolean;
    phase?: string | null;
    attempt?: number | null;
    battery?: number | null;
    last_sync_ok?: boolean | null;
    last_sync_time?: string | null;
    last_frames?: number | null;
    last_error?: string | null;
}

const DOT_COLOR: Record<string, string> = {
    ok: 'bg-green-500',
    syncing: 'bg-yellow-400 animate-pulse',
    waiting: 'bg-sky-400',
    stale: 'bg-amber-500',
    error: 'bg-red-500',
};

const LABEL: Record<string, string> = {
    ok: 'Ring synced',
    syncing: 'Syncing…',
    waiting: 'Waiting for ring',
    stale: 'Sync stale',
    error: 'Ring offline',
};

const PHASE_LABEL: Record<string, string> = {
    connecting: 'connecting',
    retry_wait: 'retrying',
    connected: 'connected',
    draining: 'pulling data',
    ingesting: 'saving',
    live: 'live',
    reconnect_wait: 'reconnecting',
    dongle_reset: 'resetting dongle',
};

function agoText(iso?: string | null): string {
    if (!iso) return 'never';
    const ms = Date.now() - new Date(iso).getTime();
    const min = Math.floor(ms / 60000);
    if (min < 1) return 'just now';
    if (min < 60) return `${min}m ago`;
    const h = Math.floor(min / 60);
    if (h < 48) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
}

export const RingStatus = () => {
    const [status, setStatus] = useState<RingStatusData | null>(null);
    const [notice, setNotice] = useState<string | null>(null);

    const refresh = useCallback(() => {
        api.getRingStatus()
            .then(setStatus)
            .catch(() => setStatus(null));
    }, []);

    useEffect(() => {
        refresh();
        const t = setInterval(refresh, 10000);
        return () => clearInterval(t);
    }, [refresh]);

    if (!status || !status.available) return null;

    const ind = status.syncing ? 'syncing' : status.indicator;
    const title = [
        LABEL[ind] || ind,
        ind === 'waiting'
            ? 'ring radio is napping — dock it ~5 s for an instant catch; data back-fills automatically'
            : (status.phase ? `phase: ${status.phase}` : null),
        status.battery != null ? `battery ${status.battery}%` : null,
        `last sync: ${agoText(status.last_sync_time)}`,
        status.last_frames != null ? `${status.last_frames} frames` : null,
        status.last_error ? `error: ${status.last_error}` : null,
    ].filter(Boolean).join(' · ');

    const onSync = () => {
        api.ringSyncNow()
            .then((r: { message?: string }) => { setNotice(r?.message || 'Sync started.'); refresh(); })
            .catch((e: unknown) => {
                setNotice(e instanceof Error ? e.message : 'Sync unavailable.');
                refresh();
            });
        setTimeout(() => setNotice(null), 10000);
    };

    return (
        <div className="flex items-center gap-2 mr-2" title={title}>
            <div className="flex items-center gap-1.5 px-2 py-1 rounded-md border border-border bg-card/50">
                <Bluetooth className="h-3.5 w-3.5 text-muted-foreground" />
                <span className={`h-2.5 w-2.5 rounded-full ${DOT_COLOR[ind] || 'bg-gray-500'}`} />
                <span className="text-xs text-muted-foreground hidden sm:inline">
                    {status.live ? 'Ring live' : (LABEL[ind] || ind)}
                    {ind === 'syncing' && status.phase && status.phase !== 'live' &&
                        ` · ${PHASE_LABEL[status.phase] || status.phase}`}
                    {status.battery != null && ` · ${status.battery}%`}
                    {ind !== 'syncing' && ` · ${agoText(status.last_sync_time)}`}
                </span>
            </div>
            {!status.live && (
                <Button
                    variant="outline"
                    size="sm"
                    className="gap-1.5"
                    onClick={onSync}
                >
                    <RefreshCw className={`h-3.5 w-3.5 ${status.syncing ? 'animate-spin' : ''}`} />
                    {status.syncing ? 'Syncing' : 'Sync now'}
                </Button>
            )}
            {notice && (
                <span className="text-[10px] text-muted-foreground max-w-[260px] leading-tight">
                    {notice}
                </span>
            )}
        </div>
    );
};

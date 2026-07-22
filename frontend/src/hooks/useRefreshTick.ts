import { useState, useEffect } from 'react';

/**
 * Global data heartbeat. Returns a counter that increments:
 *  - every `intervalMs` (default 60 s), and
 *  - whenever the window regains focus / becomes visible.
 *
 * Data hooks include it in their effect deps so every widget refetches
 * periodically while the app is open — the ringlink daemon ingests new data
 * every ~5 min, and without this the UI only updated on remount.
 */
export function useRefreshTick(intervalMs: number = 60_000): number {
    const [tick, setTick] = useState(0);

    useEffect(() => {
        const id = setInterval(() => setTick(t => t + 1), intervalMs);
        const onVisible = () => {
            if (document.visibilityState === 'visible') setTick(t => t + 1);
        };
        window.addEventListener('focus', onVisible);
        document.addEventListener('visibilitychange', onVisible);
        return () => {
            clearInterval(id);
            window.removeEventListener('focus', onVisible);
            document.removeEventListener('visibilitychange', onVisible);
        };
    }, [intervalMs]);

    return tick;
}

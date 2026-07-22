import { useState, useEffect, useRef } from 'react';

import { api } from '@/lib/api';
import { useRefreshTick } from './useRefreshTick';

interface QueryResult {
    date: string;
    value: number;
}

export function useOuraQuery(path: string, startDate?: string, endDate?: string) {
    const [data, setData] = useState<QueryResult[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const tick = useRefreshTick();
    const hasLoaded = useRef(false);

    useEffect(() => {
        if (!path) return;

        const fetchData = async () => {
            // Spinner only on first load; background refreshes are silent.
            if (!hasLoaded.current) setLoading(true);
            setError(null);
            try {
                const json = await api.getQuery(path, startDate, endDate);
                setData(json);
                hasLoaded.current = true;
            } catch (err) {
                setError(err instanceof Error ? err.message : 'Unknown error');
                console.error("Query Error:", err);
            } finally {
                setLoading(false);
            }
        };

        fetchData();
    }, [path, startDate, endDate, tick]);

    return { data, loading, error };
}

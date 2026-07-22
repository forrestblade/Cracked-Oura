const BASE_URL = 'http://localhost:8000';

export interface ChatMessage {
    role: 'user' | 'assistant';
    content: string;
    thoughts?: any[];
}

export const api = {
    // --- Manual import ---
    uploadZip: async (file: File) => {
        const formData = new FormData();
        formData.append('file', file);
        const res = await fetch(`${BASE_URL}/api/ingest/zip`, {
            method: 'POST',
            body: formData,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Upload failed');
        return data;
    },

    // --- Dashboard Data ---
    getDailyData: async (date: string) => {
        const res = await fetch(`${BASE_URL}/api/days/${date}`);
        if (!res.ok) throw new Error('Failed to fetch daily data');
        return res.json();
    },

    getQuery: async (path: string, startDate?: string, endDate?: string) => {
        const params = new URLSearchParams({ path });
        if (startDate) params.append('start_date', startDate);
        if (endDate) params.append('end_date', endDate);

        const res = await fetch(`${BASE_URL}/api/query?${params.toString()}`);
        if (!res.ok) throw new Error('Failed to fetch query data');
        return res.json();
    },

    getSchema: async () => {
        const res = await fetch(`${BASE_URL}/api/schema`);
        if (!res.ok) throw new Error('Failed to fetch schema');
        return res.json();
    },

    getTrends: async (metric: string, startDate: string, endDate: string) => {
        return api.getQuery(metric, startDate, endDate);
    },

    // --- Claude OAuth (AI analyst) ---
    getClaudeStatus: async () => {
        const res = await fetch(`${BASE_URL}/api/claude/auth/status`);
        if (!res.ok) throw new Error('Failed to fetch Claude status');
        return res.json();
    },

    startClaudeAuth: async () => {
        const res = await fetch(`${BASE_URL}/api/claude/auth/start`, { method: 'POST' });
        if (!res.ok) throw new Error('Failed to start Claude auth');
        return res.json();
    },

    finishClaudeAuth: async (code: string) => {
        const res = await fetch(`${BASE_URL}/api/claude/auth/finish`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Failed to finish Claude auth');
        }
        return res.json();
    },

    logoutClaude: async () => {
        const res = await fetch(`${BASE_URL}/api/claude/auth/logout`, { method: 'POST' });
        if (!res.ok) throw new Error('Failed to disconnect Claude');
        return res.json();
    },

    // --- Local BLE ring (ringlink) ---
    getRingStatus: async () => {
        const res = await fetch(`${BASE_URL}/api/ring/status`);
        if (!res.ok) throw new Error('Failed to fetch ring status');
        return res.json();
    },

    ringSyncNow: async () => {
        const res = await fetch(`${BASE_URL}/api/ring/sync`, { method: 'POST' });
        if (!res.ok) {
            let detail = 'Failed to start ring sync';
            try { detail = (await res.json())?.detail || detail; } catch { /* noop */ }
            throw new Error(detail);
        }
        return res.json();
    },

    // --- Layout ---
    getLayout: async () => {
        const res = await fetch(`${BASE_URL}/api/dashboard`);
        if (!res.ok) throw new Error('Failed to fetch layout');
        return res.json();
    },

    saveLayout: async (layout: any) => {
        const res = await fetch(`${BASE_URL}/api/dashboard`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(layout)
        });
        if (!res.ok) throw new Error('Failed to save layout');
        return res.json();
    },

    // --- Chat ---
    sendChatMessage: async (message: string, history: ChatMessage[], context?: any) => {
        const res = await fetch(`${BASE_URL}/api/advisor/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message, history, context })
        });
        if (!res.ok) throw new Error('Chat request failed');
        return res.json();
    }
};

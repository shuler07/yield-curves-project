import { API_BASE_URL, MOCK_DATA_ON } from '../mock_config';
import {
    AVAILABLE_DATES,
    BASE_CURVE,
    BUCKETS,
    STRESS_SCENARIOS,
    getHistoricalCurve,
    type StressScenario,
} from '../data/mockData';

export interface BucketInfo {
    id: string | number;
    label: string;
    startMonths: number;
    endMonths: number;
    midpointYears: number;
}

export interface DashboardCurveData {
    date: string;
    quoteTime?: string;
    scenarioVersion?: string | null;
    buckets: BucketInfo[];
    baseCurve: number[];
    scenarioCurves: Record<string, number[]>;
    scenarios: StressScenario[];
    availableDates: string[];
}

interface BackendPoint {
    tenor?: string | number;
    term?: string | number;
    rate?: number;
    value?: number;
    yield_rate?: number;
}

interface BackendCurve {
    scenario: string;
    scenario_version?: string | null;
    points: BackendPoint[];
}

interface BackendSnapshot {
    curve_date: string;
    quote_time?: string;
    curves?: BackendCurve[];
    points?: BackendPoint[];
}

interface BackendHistoryResponse {
    date_from: string;
    date_to: string;
    missing_dates?: string[];
    snapshots: BackendSnapshot[];
}

const cleanBaseUrl = API_BASE_URL.replace(/\/$/, '');

const SCENARIO_META: Record<
    string,
    { name: string; nameRu: string; color: string }
> = {
    parallel_up: {
        name: 'Parallel Up',
        nameRu: 'Параллельный сдвиг вверх',
        color: '#ef4444',
    },
    parallel_down: {
        name: 'Parallel Down',
        nameRu: 'Параллельный сдвиг вниз',
        color: '#3b82f6',
    },
    steepener: {
        name: 'Steepener',
        nameRu: 'Стипенер',
        color: '#f59e0b',
    },
    flattener: {
        name: 'Flattener',
        nameRu: 'Флэттенер',
        color: '#10b981',
    },
    short_up: {
        name: 'Short Up',
        nameRu: 'Рост короткого конца',
        color: '#a855f7',
    },
    short_down: {
        name: 'Short Down',
        nameRu: 'Снижение короткого конца',
        color: '#06b6d4',
    },
};

function normalizeScenarioId(rawName: string): string {
    const normalized = rawName.trim().toLowerCase().replace(/\s+/g, '_');
    if (normalized === 'baseline' || normalized === 'base') {
        return 'baseline';
    }
    return normalized;
}

function extractPointValue(pt: BackendPoint): number {
    const val = pt.rate ?? pt.value ?? pt.yield_rate ?? 0;
    return Number.isFinite(Number(val)) ? Number(val) : 0;
}

function buildBucketsFromPoints(points: BackendPoint[]): BucketInfo[] {
    if (!points.length) return BUCKETS;
    return points.map((pt, idx) => {
        const fallback = BUCKETS[idx];
        const tenorLabel = String(
            pt.tenor ?? pt.term ?? fallback?.label ?? `T${idx + 1}`
        );
        return {
            id: fallback ? fallback.id : `bucket_${idx}`,
            label: tenorLabel,
            startMonths: fallback ? fallback.startMonths : 0,
            endMonths: fallback ? fallback.endMonths : 0,
            midpointYears: fallback ? fallback.midpointYears : idx + 1,
        };
    });
}

async function fetchBackendJson<T>(path: string, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${cleanBaseUrl}${path}`, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        signal,
    });

    if (!response.ok) {
        const detail = await response.text().catch(() => '');
        throw new Error(
            `Ошибка API ${path}: ${response.status} ${response.statusText}${
                detail ? ` — ${detail}` : ''
            }`
        );
    }

    return response.json() as Promise<T>;
}

function buildMockCurveData(date: string): DashboardCurveData {
    const baseCurve = date === '2026-01-15' ? BASE_CURVE : getHistoricalCurve(date);
    const scenarioCurves: Record<string, number[]> = {};

    for (const scenario of STRESS_SCENARIOS) {
        scenarioCurves[scenario.id] = baseCurve.map(
            (rate, idx) => +(rate + (scenario.shifts[idx] ?? 0) / 100).toFixed(4)
        );
    }

    return {
        date,
        buckets: BUCKETS,
        baseCurve,
        scenarioCurves,
        scenarios: STRESS_SCENARIOS,
        availableDates: AVAILABLE_DATES,
    };
}

export async function fetchAvailableDates(
    params: { dateFrom?: string; dateTo?: string } = {},
    signal?: AbortSignal
): Promise<string[]> {
    if (MOCK_DATA_ON) {
        return AVAILABLE_DATES;
    }

    const dateFrom = params.dateFrom ?? '2025-01-01';
    const dateTo = params.dateTo ?? new Date().toISOString().slice(0, 10);
    const query = new URLSearchParams({ date_from: dateFrom, date_to: dateTo });

    const data = await fetchBackendJson<BackendHistoryResponse>(
        `/curves/processed?${query.toString()}`,
        signal
    );

    const snapshotDates = Array.from(
        new Set((data.snapshots ?? []).map((s) => s.curve_date))
    );
    return snapshotDates.length > 0 ? snapshotDates : AVAILABLE_DATES;
}

export async function fetchProcessedCurveByDate(
    date: string,
    scenarioVersion?: string,
    signal?: AbortSignal
): Promise<DashboardCurveData> {
    if (MOCK_DATA_ON) {
        return buildMockCurveData(date);
    }

    const query = new URLSearchParams({ date });
    if (scenarioVersion) {
        query.set('scenario_version', scenarioVersion);
    }

    const data = await fetchBackendJson<BackendHistoryResponse>(
        `/curves/processed?${query.toString()}`,
        signal
    );

    const snapshot = data.snapshots?.[0];
    if (!snapshot || !snapshot.curves?.length) {
        throw new Error(`Нет расчётных данных по кривой за дату ${date}`);
    }

    const baselineCurveObj = snapshot.curves.find(
        (c) => normalizeScenarioId(c.scenario) === 'baseline'
    );
    const basePoints = baselineCurveObj?.points ?? snapshot.curves[0].points ?? [];
    const baseCurve = basePoints.map(extractPointValue);
    const buckets: BucketInfo[] =
        basePoints.length === BUCKETS.length
            ? BUCKETS
            : buildBucketsFromPoints(basePoints);

    const scenarioCurves: Record<string, number[]> = {};
    const mappedScenarios: StressScenario[] = [];

    for (const curve of snapshot.curves) {
        const id = normalizeScenarioId(curve.scenario);
        if (id === 'baseline') continue;

        const values = curve.points.map(extractPointValue);
        scenarioCurves[id] = values;

        const fallbackMock = STRESS_SCENARIOS.find(
            (s) =>
                normalizeScenarioId(s.id) === id || normalizeScenarioId(s.name) === id
        );
        const meta = SCENARIO_META[id];

        const shifts = values.map(
            (v, idx) => Math.round((v - (baseCurve[idx] ?? 0)) * 100 * 100) / 100
        );

        mappedScenarios.push({
            id,
            name: meta ? meta.name : fallbackMock ? fallbackMock.name : curve.scenario,
            nameRu: meta
                ? meta.nameRu
                : fallbackMock
                    ? fallbackMock.nameRu
                    : curve.scenario,
            color: meta ? meta.color : fallbackMock ? fallbackMock.color : '#38bdf8',
            shifts,
        });
    }

    return {
        date: snapshot.curve_date || date,
        quoteTime: snapshot.quote_time,
        scenarioVersion:
            baselineCurveObj?.scenario_version ??
            snapshot.curves.find((c) => c.scenario_version)?.scenario_version ??
            null,
        buckets,
        baseCurve,
        scenarioCurves,
        scenarios: mappedScenarios.length > 0 ? mappedScenarios : STRESS_SCENARIOS,
        availableDates: AVAILABLE_DATES,
    };
}

export async function fetchRawCurveByDate(
    date: string,
    signal?: AbortSignal
): Promise<BackendHistoryResponse> {
    const query = new URLSearchParams({ date });
    return fetchBackendJson<BackendHistoryResponse>(
        `/curves/raw?${query.toString()}`,
        signal
    );
}
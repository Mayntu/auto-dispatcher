import { m } from '../i18n/msg';
import { edges, edgeVmax, nodeNames, nodes, turnouts } from '../data/network';

/**
 * Участок строится из таблиц nodes/edges: раздельные пункты — узлы, перегоны — рёбра.
 * Планировщик работает с участком-цепочкой, поэтому узлы упорядочиваются обходом от конечного узла.
 */
function buildSection() {
  const degree = new Map(nodes.map((n) => [n.id, 0]));
  for (const e of edges) {
    degree.set(e.start_node_id, degree.get(e.start_node_id) + 1);
    degree.set(e.end_node_id, degree.get(e.end_node_id) + 1);
  }
  const ends = nodes.filter((n) => degree.get(n.id) === 1).sort((a, b) => a.pos_x - b.pos_x);
  if (ends.length !== 2 || edges.length !== nodes.length - 1) throw new Error('Участок должен быть цепочкой узлов');

  const chain = [ends[0]];
  const chainEdges = [];
  while (chain.length < nodes.length) {
    const cur = chain[chain.length - 1];
    const e = edges.find((x) => !chainEdges.includes(x) && (x.start_node_id === cur.id || x.end_node_id === cur.id));
    chainEdges.push(e);
    chain.push(nodes.find((n) => n.id === (e.start_node_id === cur.id ? e.end_node_id : e.start_node_id)));
  }

  let km = 0;
  const stations = chain.map((n, i) => {
    if (i > 0) km += chainEdges[i - 1].length / 1000;
    const terminal = degree.get(n.id) === 1;
    const isStation = n.tracks_amount > 2 || terminal;
    const name = nodeNames[n.id] ?? `Узел ${n.id}`;
    return {
      id: n.id,
      short: name,
      name: `${isStation ? 'Ст.' : 'Рзд.'} ${name}`,
      km: Math.round(km * 10) / 10,
      tracks: n.tracks_amount,
      kind: isStation ? 'station' : 'passing',
      terminal,
      x: n.pos_x,
      y: n.pos_y,
    };
  });

  const segments = chainEdges.map((e, i) => ({
    index: i,
    edgeId: e.id,
    from: i,
    to: i + 1,
    /** Ребро в БД может быть направлено «против» цепочки — тогда позиции на нём считаются от другого конца. */
    reversed: e.start_node_id !== chain[i].id,
    length: e.length,
    lengthKm: e.length / 1000,
    tracks: e.tracks_amount,
    vmax: edgeVmax[e.id] ?? 80,
    turnouts: turnouts.filter((t) => t.edge_id === e.id),
    /** Съезды делят каждый путь перегона на части: доли от станции from, по возрастанию. */
    cuts: turnouts
      .filter((t) => t.edge_id === e.id)
      .map((t) => (e.start_node_id !== chain[i].id ? 1 - t.pos / e.length : t.pos / e.length))
      .sort((a, b) => a - b),
  }));
  return { stations, segments };
}

const built = buildSection();
const STATIONS = built.stations;

export const SECTION = { name: m('Участок {a} — {b}', { a: m(STATIONS[0].short), b: m(STATIONS.at(-1).short) }), ...built };
export let SECTION_KM = STATIONS[STATIONS.length - 1].km;

/** Категория поезда сервера (§9.2) → категория интерфейса. */
export const CATEGORY_FROM_SERVER = { express: 'express', passenger: 'pass', freight: 'freight' };

/**
 * Участок с сервера (GET /api/infra: {infra: {stations, segments, blocks, signals}, categories, timetable, …}).
 * Пересобирает SECTION на месте — все модули держат ссылку на те же массивы. Вызывается до первого рендера.
 * Координат в инфраструктуре сервера нет — схема раскладывается по километражу «змейкой», как на карте.
 */
export function applyInfra(res) {
  const infra = res.infra ?? res;
  // Цвета, скорости и названия категорий — с сервера (§9.2, §18.3).
  for (const c of res.categories ?? []) {
    const ui = CATEGORIES[CATEGORY_FROM_SERVER[c.id]];
    if (ui) Object.assign(ui, { name: c.name, color: c.color, vmax: c.v_max_kmh, massT: c.mass_t });
  }
  const byId = new Map(infra.stations.map((s) => [s.id, s]));
  const order = res.station_order?.length ? res.station_order.map((id) => byId.get(id)).filter(Boolean) : [...infra.stations].sort((a, b) => a.km - b.km);
  const stations = order.map((st, i) => {
    const terminal = i === 0 || i === order.length - 1;
    // Короткое имя для подписей: у станции без «Ст.»; разъезд без префикса («1») не читается — оставляем «Рзд. 1».
    const short = st.kind === 'siding' ? st.name : st.name.replace(/^Ст\.\s*/, '');
    return {
      id: i + 1,
      specId: st.id,
      short,
      name: st.name,
      km: st.km,
      tracks: st.tracks.length,
      trackIds: st.tracks.map((t) => t.id),
      trackInfo: st.tracks,
      kind: st.kind === 'siding' ? 'passing' : 'station',
      terminal,
      x: st.km * 1000,
      y: (i % 2 === 0 ? -1 : 1) * 1800,
    };
  });
  const segments = order.slice(0, -1).map((a, i) => {
    const b = order[i + 1];
    const g = infra.segments.find((x) => (x.from_station === a.id && x.to_station === b.id) || (x.from_station === b.id && x.to_station === a.id));
    const length = g?.length_m ?? Math.round((b.km - a.km) * 1000);
    return {
      index: i,
      edgeId: g?.id ?? `${a.id}-${b.id}`,
      specId: g?.id,
      from: i,
      to: i + 1,
      reversed: g ? g.from_station !== a.id : false,
      length,
      lengthKm: length / 1000,
      tracks: 1,
      vmax: g?.v_max_kmh ?? 80,
      speedZones: g?.speed_zones ?? [],
      gradeZones: g?.grade_zones ?? [],
      blocks: (infra.blocks ?? []).filter((x) => x.segment_id === g?.id).sort((p, q) => p.index - q.index),
      signals: (infra.signals ?? []).filter((x) => x.segment_id === g?.id),
      turnouts: [],
      cuts: [],
    };
  });
  STATIONS.splice(0, STATIONS.length, ...stations);
  SECTION.segments.splice(0, SECTION.segments.length, ...segments);
  SECTION.signals = infra.signals ?? [];
  SECTION.server = { epoch: res.sim_epoch, timetable: res.timetable ?? [], thresholds: res.thresholds, intervals: res.intervals };
  SECTION.name = m('Участок {a} — {b}', { a: m(STATIONS[0].short), b: m(STATIONS.at(-1).short) });
  SECTION.fromServer = true;
  SECTION_KM = STATIONS[STATIONS.length - 1].km;
}

/** Позиция на ребре (метры от start_node) → доля пути от станции from к станции to перегона. */
export const edgeFraction = (seg, pos) => (seg.reversed ? 1 - pos / seg.length : pos / seg.length);

export const CATEGORIES = {
  express: { id: 'express', name: 'Скорый', short: 'Скор.', vmax: 140, massT: 500, color: '#B39DDB' },
  pass: { id: 'pass', name: 'Пассажирский', short: 'Пасс.', vmax: 100, massT: 700, color: '#4FC3F7' },
  suburb: { id: 'suburb', name: 'Пригородный', short: 'Приг.', vmax: 90, massT: 450, color: '#81C784' },
  freightFast: { id: 'freightFast', name: 'Грузовой ускоренный', short: 'Гр. уск.', vmax: 80, massT: 3200, color: '#FFB74D' },
  freight: { id: 'freight', name: 'Грузовой', short: 'Груз.', vmax: 60, massT: 5200, color: '#F48FB1' },
};

const hm = (h, m) => h * 3600 + m * 60;

/** Момент старта симуляции. */
export const SIM_START = hm(7, 57);

export function initialTrains() {
  // Плотный график на ~6 часов: в каждую сторону поезд каждые 15–25 мин, смесь категорий.
  const S = [1, 2, 3, 4];
  const R = [4, 3, 2, 1];
  return [
    // нечётное направление (Северная → Южная)
    { id: '6101', number: '6101', category: 'suburb', dir: 1, departure: hm(8, 0), stops: S, dwell: 60 },
    { id: '3401', number: '3401', category: 'freight', dir: 1, departure: hm(8, 12), stops: [], dwell: 0 },
    { id: '701', number: '701', category: 'pass', dir: 1, departure: hm(8, 35), stops: [2], dwell: 120 },
    { id: '2201', number: '2201', category: 'freightFast', dir: 1, departure: hm(8, 55), stops: [], dwell: 0 },
    { id: '6103', number: '6103', category: 'suburb', dir: 1, departure: hm(9, 15), stops: S, dwell: 60 },
    { id: '3405', number: '3405', category: 'freight', dir: 1, departure: hm(9, 35), stops: [], dwell: 0 },
    { id: '703', number: '703', category: 'pass', dir: 1, departure: hm(10, 0), stops: [2], dwell: 120 },
    { id: '2203', number: '2203', category: 'freightFast', dir: 1, departure: hm(10, 20), stops: [], dwell: 0 },
    { id: '6105', number: '6105', category: 'suburb', dir: 1, departure: hm(10, 40), stops: S, dwell: 60 },
    {
      id: '3403',
      number: '3403',
      category: 'freight',
      dir: 1,
      departure: hm(11, 0),
      stops: [],
      dwell: 0,
      vmaxOverride: 30,
      note: 'Ограничение 30 км/ч (негабаритный груз)',
    },
    { id: '705', number: '705', category: 'pass', dir: 1, departure: hm(11, 30), stops: [2], dwell: 120 },
    { id: '3407', number: '3407', category: 'freight', dir: 1, departure: hm(11, 55), stops: [], dwell: 0 },
    { id: '6107', number: '6107', category: 'suburb', dir: 1, departure: hm(12, 20), stops: S, dwell: 60 },
    // чётное направление (Южная → Северная)
    { id: '3402', number: '3402', category: 'freight', dir: -1, departure: hm(8, 5), stops: [], dwell: 0 },
    { id: '6102', number: '6102', category: 'suburb', dir: -1, departure: hm(8, 25), stops: R, dwell: 60 },
    { id: '2202', number: '2202', category: 'freightFast', dir: -1, departure: hm(8, 45), stops: [], dwell: 0 },
    { id: '702', number: '702', category: 'pass', dir: -1, departure: hm(9, 5), stops: [2], dwell: 120 },
    { id: '3404', number: '3404', category: 'freight', dir: -1, departure: hm(9, 25), stops: [], dwell: 0 },
    { id: '6104', number: '6104', category: 'suburb', dir: -1, departure: hm(9, 50), stops: R, dwell: 60 },
    { id: '3406', number: '3406', category: 'freight', dir: -1, departure: hm(10, 10), stops: [], dwell: 0 },
    { id: '704', number: '704', category: 'pass', dir: -1, departure: hm(10, 35), stops: [2], dwell: 120 },
    { id: '2204', number: '2204', category: 'freightFast', dir: -1, departure: hm(10, 55), stops: [], dwell: 0 },
    { id: '6106', number: '6106', category: 'suburb', dir: -1, departure: hm(11, 20), stops: R, dwell: 60 },
    { id: '3408', number: '3408', category: 'freight', dir: -1, departure: hm(11, 45), stops: [], dwell: 0 },
    { id: '706', number: '706', category: 'pass', dir: -1, departure: hm(12, 10), stops: [2], dwell: 120 },
    { id: '6108', number: '6108', category: 'suburb', dir: -1, departure: hm(12, 35), stops: R, dwell: 60 },
  ];
}

/** Название раздельного пункта и перегона — сообщениями: переводит интерфейс. */
export const stationName = (i) =>
  m(STATIONS[i]?.kind === 'station' ? 'Ст. {name}' : 'Рзд. {name}', { name: m(STATIONS[i]?.short ?? `#${i}`) });
export const segmentName = (i) => m('{a} — {b}', { a: m(STATIONS[i].short), b: m(STATIONS[i + 1].short) });

/** Часть пути между съездами, в которую попадает доля f перегона: [начало, конец]. */
export function partOf(seg, f) {
  let lo = 0;
  let hi = 1;
  for (const c of seg.cuts) {
    if (c <= f) lo = c;
    else {
      hi = c;
      break;
    }
  }
  return [lo, hi];
}

/** Какие части пути закрывает перекрытие pos_start…pos_end: поезд объезжает их по соседнему пути через съезды. */
export function blockRange(seg, posStart, posEnd) {
  const a = edgeFraction(seg, posStart);
  const b = edgeFraction(seg, posEnd);
  return [partOf(seg, Math.min(a, b))[0], partOf(seg, Math.max(a, b))[1]];
}

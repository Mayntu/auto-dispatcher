/**
 * Mock-данные в формате БД: таблицы nodes, edges, turnouts, blockages (поля — как в схеме).
 * Единицы: координаты и длины — метры.
 */
export const nodes = [
  { id: 1, pos_x: 0, pos_y: 0, tracks_amount: 4 },
  { id: 2, pos_x: 17000, pos_y: -2600, tracks_amount: 2 },
  { id: 3, pos_x: 35500, pos_y: -1200, tracks_amount: 3 },
  { id: 4, pos_x: 53000, pos_y: 2400, tracks_amount: 2 },
  { id: 5, pos_x: 74000, pos_y: 1600, tracks_amount: 2 },
  { id: 6, pos_x: 96000, pos_y: -1400, tracks_amount: 4 },
];

export const edges = [
  { id: 1, start_node_id: 1, end_node_id: 2, length: 17000, tracks_amount: 2 },
  { id: 2, start_node_id: 2, end_node_id: 3, length: 19000, tracks_amount: 2 },
  { id: 3, start_node_id: 3, end_node_id: 4, length: 19000, tracks_amount: 2 },
  { id: 4, start_node_id: 4, end_node_id: 5, length: 22000, tracks_amount: 2 },
  { id: 5, start_node_id: 5, end_node_id: 6, length: 23000, tracks_amount: 2 },
];

/**
 * Стрелочные переводы. На двухпутных перегонах стоят парами — перекрёстные съезды (X между путями),
 * по ним поезд уходит на соседний путь, если свой закрыт. pos — метры от start_node.
 */
export const turnouts = [
  { id: 1, edge_id: 1, pos: 5500 },
  { id: 2, edge_id: 1, pos: 11500 },
  { id: 3, edge_id: 2, pos: 6300 },
  { id: 4, edge_id: 2, pos: 12700 },
  { id: 5, edge_id: 3, pos: 6300 },
  { id: 6, edge_id: 3, pos: 12700 },
  { id: 7, edge_id: 4, pos: 7300 },
  { id: 8, edge_id: 4, pos: 14700 },
  { id: 9, edge_id: 5, pos: 7700 },
  { id: 10, edge_id: 5, pos: 15300 },
];

/** Действующие перекрытия на старте — пусто; новые появляются, когда диспетчер бросает событие на перегон. */
export const blockages = [];

/** Справочник интерфейса (в схеме БД этих полей нет): названия узлов и допустимая скорость на перегонах. */
export const nodeNames = {
  1: 'Северная',
  2: 'Берёзовый',
  3: 'Озёрная',
  4: 'Каменный',
  5: 'Луговой',
  6: 'Южная',
};
export const edgeVmax = { 1: 100, 2: 100, 3: 90, 4: 100, 5: 90 };

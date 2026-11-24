'use strict';
const get = id => document.getElementById(id);
const data = JSON.parse(get('data').textContent);
const states = {open: '개방', blocked: '차단', review: '검토'};
const colors = {open:'#127349', blocked:'#bd3239', review:'#b16117'};
const roadColors = [colors.open, colors.review, colors.blocked];
const scenarioNames = {S0: '기준망', S1: '0.5m 이상 침수', S2: '전체 침수구역'};
const methods = {automatic_projection: '자동 투영', manual_keep: '수동 검토 유지',
  manual_entrance_refinement: '표시된 입구 기반 정밀 보정'};
const reviews = {keep: '유지', revise: '보정', defer: '보류'};
const pointFlood = {inside: '내부', boundary: '경계', outside: '외부'};
const numbers = new Intl.NumberFormat('ko-KR');
const decimal = value => Number(value).toLocaleString('ko-KR', {maximumFractionDigits:2});
const node = (tag, value, className) => {
  const e = document.createElement(tag);
  if (value !== undefined) e.textContent = value;
  if (className) e.className = className;
  return e;
};
const checked = id => get(id).checked;
const latlon = (site, prefix) => [Number(site[prefix + '_latitude']), Number(site[prefix + '_longitude'])];
const bounds = L.latLngBounds(data.bounds);
const map = L.map('map', {minZoom: 7, maxZoom: 20, zoomAnimation: false});
map.attributionControl.setPrefix(false);
map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>');
L.control.scale({imperial: false, position: 'bottomleft'}).addTo(map);
function fitRegion() {
  map.fitBounds(bounds, {padding: [16,16], maxZoom: 16, animate: false});
}
fitRegion();

let scenario = 'S0';
let activeSite = null;
let geometry = null;
let serial = 0;
let frame = 0;
let mapError = '';
let tileError = false;
const cached = new Map();
const mapLayer = map.createPane('scenarioCanvas');
mapLayer.style.zIndex = '350';
mapLayer.style.pointerEvents = 'none';
const canvas = node('canvas', undefined, 'study-canvas');
mapLayer.append(canvas);
const ctx = canvas.getContext('2d');
const connectors = L.layerGroup().addTo(map);
const markers = L.layerGroup().addTo(map);
const tile = L.tileLayer('https://tiles.maps.eox.at/wmts/1.0.0/terrain-light_3857/default/g/{z}/{y}/{x}.jpg', {
  maxZoom:20, maxNativeZoom:14, noWrap:true, keepBuffer:1, updateWhenIdle:true,
  attribution: '<a href="https://maps.eox.at/" target="_blank" rel="noopener noreferrer">EOX Terrain Light</a> · 데이터 © OpenStreetMap contributors 및 기타 제공자'
});
function showMapMessage() {
  const e = get('map-message');
  const text = mapError || (!geometry ? '시나리오 도형을 불러오는 중입니다.' :
    tileError ? '온라인 배경지도를 불러올 수 없습니다. 저장된 도로와 시설은 계속 사용할 수 있습니다.' : '');
  e.hidden = !text;
  e.textContent = text;
}
tile.on('tileerror', () => { tileError = map.hasLayer(tile); showMapMessage(); });
tile.on('tileload', () => { tileError = false; showMapMessage(); });
get('basemap').addEventListener('click', () => {
  const enabled = !map.hasLayer(tile);
  if (enabled) tile.addTo(map);
  else { tile.remove(); tileError = false; showMapMessage(); }
  get('basemap').setAttribute('aria-pressed', String(enabled));
});
get('extent').addEventListener('click', fitRegion);

async function decode(id) {
  const encoded = get(id).textContent.trim();
  if (!encoded) return [];
  if (typeof DecompressionStream !== 'function') {
    throw new Error('이 브라우저는 압축 지도 데이터 읽기를 지원하지 않습니다. 최신 Edge 또는 Chrome에서 열어 주세요.');
  }
  const bytes = Uint8Array.from(atob(encoded), code => code.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).json();
}
function extent(coords) {
  let left = Infinity, bottom = Infinity, right = -Infinity, top = -Infinity;
  for (let i = 0; i < coords.length; i += 2) {
    const x = coords[i], y = coords[i+1];
    left = Math.min(left, x); right = Math.max(right, x);
    bottom = Math.min(bottom, y); top = Math.max(top, y);
  }
  return [left,bottom,right,top];
}
async function readScenario(name) {
  if (!cached.has(name)) {
    const roads = decode('roads-' + name);
    const mask = name === 'S0' ? Promise.resolve([]) : decode('mask-' + name);
    const promise = Promise.all([roads,mask]).then(([rows, areas]) => {
      const paths = [[],[],[]];
      for (const [state,type,coords] of rows) paths[state].push({type, coords, box:extent(coords)});
      return {paths, polygons:areas.map(rings => ({rings, box:extent(rings[0])}))};
    });
    cached.set(name, promise);
    promise.catch(() => cached.delete(name));
  }
  return cached.get(name);
}
function schedule() {
  if (!frame) frame = requestAnimationFrame(draw);
}
const HALF_WORLD = Math.PI * 6378137;
function draw() {
  frame = 0;
  const size = map.getSize(), dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.round(size.x*dpr), height = Math.round(size.y*dpr);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width; canvas.height = height;
    canvas.style.width = size.x+'px'; canvas.style.height = size.y+'px';
  }
  const origin = map.containerPointToLayerPoint([0,0]);
  L.DomUtil.setPosition(canvas, origin);
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,size.x,size.y);
  if (!geometry) return;

  const scale = map.options.crs.scale(map.getZoom()) / (2 * HALF_WORLD);
  const pixel = map.getPixelOrigin().add(origin);
  const xShift = HALF_WORLD * scale - pixel.x, yShift = HALF_WORLD * scale - pixel.y;
  const minX = (-xShift - 3)/scale, maxX = (size.x - xShift + 3)/scale;
  const maxY = (yShift + 3)/scale, minY = (yShift - size.y - 3)/scale;
  const visible = ([left,bottom,right,top]) => right >= minX && left <= maxX && top >= minY && bottom <= maxY;
  function append(coords, close = false) {
    ctx.moveTo(coords[0]*scale+xShift, yShift-coords[1]*scale);
    for (let i = 2; i < coords.length; i += 2) ctx.lineTo(coords[i]*scale+xShift, yShift-coords[i+1]*scale);
    if (close) ctx.closePath();
  }
  if (checked('layer-flood')) {
    ctx.fillStyle = 'rgba(66,154,209,.25)'; ctx.strokeStyle = 'rgba(26,112,166,.55)'; ctx.lineWidth = 0.7;
    for (const {rings,box} of geometry.polygons) {
      if (!visible(box)) continue;
      ctx.beginPath();
      for (const ring of rings) append(ring, true);
      ctx.fill('evenodd');
      ctx.stroke();
    }
  }
  const type = get('road-type').value;
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  // Draw cut and review intervals over the network for visibility.
  for (const status of [0,2,1]) {
    if (!checked('layer-' + status)) continue;
    ctx.beginPath();
    for (const path of geometry.paths[status]) {
      if ((type === '' || path.type === Number(type)) && visible(path.box)) append(path.coords);
    }
    ctx.strokeStyle = roadColors[status];
    ctx.globalAlpha = status === 0 ? .78 : .96;
    ctx.lineWidth = status === 0 ? (map.getZoom() >= 13 ? 1.5 : 1) : (map.getZoom() >= 13 ? 3 : 2);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}
map.on('move zoom resize viewreset', schedule);

function filteredSites() {
  const county = get('county').value;
  const state = get('facility-state').value;
  const query = get('search').value.trim().toLocaleLowerCase('ko-KR');
  return data.sites.filter(s => (!county || s.county === county)
    && (!state || s.scenarios[scenario].point_state === state)
    && (!query || [s.facility_name,s.county,s.facility_id].some(v => v.toLocaleLowerCase('ko-KR').includes(query))));
}
function selectSite(site, zoom) {
  activeSite = site;
  if (zoom) map.fitBounds([latlon(site,'facility'),latlon(site,'final_projection')],
                         {padding:[65,65], maxZoom:16, animate:false});
  renderFacilities();
}
function detail() {
  const e = get('details');
  if (!activeSite) {
    e.replaceChildren(node('h2','시설 상세'), node('p','지도나 목록에서 시설을 선택하세요.','muted'));
    return;
  }
  const f = activeSite, d = f.scenarios[scenario];
  const coords = prefix => latlon(f,prefix).map(v => v.toFixed(7)).join(', ');
  const list = node('dl');
  const fields = [
    ['시설 ID', f.county+' · '+f.facility_id], ['시나리오', scenario],
    ['접속점 상태', states[d.point_state]], ['시설 좌표', coords('facility')],
    ['접속점 좌표', coords('final_projection')], ['접속 거리', decimal(f.final_connector_length_m)+' m'],
    ['선정 방식', methods[f.selection_method]], ['수동 검토', reviews[f.review_decision] || '미검토'],
    ['검토 근거', [f.review_reason,f.review_notes].filter(Boolean).join(' · ') || '—'],
    ['도로 유형', f.highway+' · '+(Number(f.s0_direction_count) === 1 ? '단방향' : '양방향')],
    ['대표점 침수', scenario === 'S0' ? '침수 적용 없음' : pointFlood[d.facility_point_mask_state]],
    ['직선 중첩', decimal(d.straight_connector_interior_overlap_m)+' m'],
    ['도착 링크', numbers.format(d.arrival_count_review_closed)+'개'
      + (d.arrival_count_review_open === d.arrival_count_review_closed ? '' :
         ' · 검토 구간 허용 시 '+numbers.format(d.arrival_count_review_open)+'개')],
    ['차량 진입', '미확인 (unverified)'],
  ];
  for (const [name,val] of fields) list.append(node('dt',name),node('dd',val));
  e.replaceChildren(node('h2',f.facility_name),node('p',f.address,'muted'),list);
}
function renderFacilities() {
  const items = filteredSites();
  if (!items.includes(activeSite)) activeSite = null;
  get('result-count').textContent = numbers.format(items.length)+'곳';
  connectors.clearLayers();
  markers.clearLayers();
  const buttons = [];
  for (const f of items) {
    const state = f.scenarios[scenario].point_state;
    const color = colors[state];
    const button = node('button');
    button.type = 'button';
    button.setAttribute('aria-current', String(activeSite === f));
    button.append(node('strong',f.facility_name),node('span',states[state],'pill '+state),
                  node('small',f.county+' · '+f.facility_id));
    button.addEventListener('click', () => selectSite(f,true));
    const li = node('li');
    li.append(button);
    buttons.push(li);
    const site = latlon(f,'facility'), access = latlon(f,'final_projection');
    if (checked('layer-connector')) {
      L.polyline([site,access],{color, opacity:.75, weight:2, dashArray:'4 4', interactive:false})
        .addTo(connectors);
    }
    for (const [enabled, position, role, filled] of [
      [checked('layer-facility'), site,'시설 대표점',true],
      [checked('layer-access'), access,'도로 접속점',false],
    ]) {
      if (!enabled) continue;
      L.circleMarker(position,{radius:(activeSite === f ? 8 : 5),color:filled ? '#fff' : color,weight:filled ? 1 : 2,
        fillColor:filled ? color : '#fff',fillOpacity:1})
        .bindTooltip(node('span',f.facility_name+' · '+role))
        .on('click', () => selectSite(f,true)).addTo(markers);
    }
  }
  if (!buttons.length) buttons.push(node('li','조건에 맞는 시설이 없습니다.','empty'));
  get('facility-list').replaceChildren(...buttons);
  detail();
}
function fillSummary() {
  const stats = data.stats[scenario];
  const values = {
    facilities:numbers.format(stats.facilities), open:numbers.format(stats.open), blocked:numbers.format(stats.blocked),
    road:decimal(stats.physical_blocked_m/1000)+' km', area:decimal(stats.flood_km2)+' km²',
    scc:numbers.format(stats.largest_scc_original_nodes), point:numbers.format(stats.facility_inside),
    line:numbers.format(stats.connector_crossings),
  };
  for (const [key,value] of Object.entries(values)) get('stat-'+key).textContent = value;
}
async function selectScenario(name) {
  const request = ++serial;
  scenario = name;
  geometry = null;
  mapError = '';
  for (const button of document.querySelectorAll('[data-scenario]')) {
    button.setAttribute('aria-pressed', String(button.dataset.scenario === name));
  }
  get('map-name').textContent = name+' '+scenarioNames[name];
  get('layer-flood').disabled = name === 'S0' || !data.showMasks;
  get('mask-notice').hidden = data.showMasks || name === 'S0';
  get('mask-source').hidden = name === 'S0';
  get('mask-notice').textContent = '공유판 침수 도형은 이용 조건 확인 후 포함됩니다.';
  fillSummary();
  renderFacilities();
  showMapMessage();
  schedule();
  try {
    const loaded = await readScenario(name);
    if (serial !== request) return;
    geometry = loaded;
  } catch (error) {
    if (serial !== request) return;
    mapError = error.message;
  }
  showMapMessage();
  schedule();
}

for (const county of [...new Set(data.sites.map(f => f.county))].sort()) {
  const option = node('option',county); option.value = county; get('county').append(option);
}
for (const [i,type] of data.roadTypes.entries()) {
  const option = node('option',type); option.value = String(i); get('road-type').append(option);
}
for (const button of document.querySelectorAll('[data-scenario]')) {
  button.addEventListener('click', () => selectScenario(button.dataset.scenario));
}
for (const id of ['layer-flood','layer-0','layer-1','layer-2','road-type']) get(id).addEventListener('change', schedule);
for (const id of ['county','facility-state','layer-facility','layer-access','layer-connector']) {
  get(id).addEventListener('change', renderFacilities);
}
get('search').addEventListener('input', renderFacilities);
selectScenario('S0');

'use strict';

// Load optional MapLibre assets after the embedded Leaflet map can already draw.
const mapLibreAssets = [
  ['link', 'maplibre-css', 'https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.1/dist/maplibre-gl.css',
    'sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK'],
  ['script', 'maplibre-js', 'https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.1/dist/maplibre-gl.js',
    'sha384-nXMl9YiLoSG5KBPTFgvxVuIqQQcqIvo0bFrvoEaGJC6r7IibIzF1S36Zbf/sKBtL'],
];

function loadBasemapAsset([tag, id, url, integrity], signal) {
  if (document.getElementById(id)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const asset = document.createElement(tag);
    function abort() {
      asset.onload = asset.onerror = null;
      asset.remove();
      reject(signal.reason);
    }
    asset.onload = () => {
      signal.removeEventListener('abort', abort);
      asset.id = id;
      resolve();
    };
    asset.onerror = () => {
      signal.removeEventListener('abort', abort);
      asset.remove();
      reject(new Error('배경지도 구성 요소를 불러올 수 없습니다.'));
    };
    if (signal.aborted) {
      abort();
      return;
    }
    signal.addEventListener('abort', abort, {once:true});
    asset.crossOrigin = 'anonymous';
    asset.integrity = integrity;
    if (tag === 'link') {
      asset.rel = 'stylesheet';
      asset.href = url;
    } else {
      asset.src = url;
    }
    document.head.append(asset);
  });
}

function attachOnlineBasemap(map, button, message, style, showOffline) {
  const pane = map.createPane('onlineBasemap');
  pane.style.zIndex = '200';
  pane.style.pointerEvents = 'none';
  const credit = '<a href="https://openfreemap.org/" target="_blank" rel="noopener noreferrer">OpenFreeMap</a> · ' +
    '<a href="https://openmaptiles.org/" target="_blank" rel="noopener noreferrer">© OpenMapTiles</a>';
  const deadlineMs = 12000;
  const providerOrigins = [new URL(style).origin, 'https://assets.openfreemap.com'];
  let session = null;
  let sequence = 0;

  function updateControl(mode, text = '') {
    button.dataset.mode = mode;
    button.setAttribute('aria-pressed', String(mode === 'online' || mode === 'connecting'));
    button.textContent = {
      online:'배경지도 끄기', connecting:'연결 취소', offline:'배경지도 다시 연결', hidden:'배경지도 켜기',
    }[mode];
    message.hidden = !text;
    message.textContent = text;
  }

  function release(attempt) {
    clearTimeout(attempt.timeout);
    attempt.controller.abort();
    if (attempt.update) map.off('move zoom resize', attempt.update);
    if (attempt.frame) cancelAnimationFrame(attempt.frame);
    if (attempt.ready) map.attributionControl.removeAttribution(credit);
    if (attempt.renderer) attempt.renderer.remove();
    if (attempt.protocol) maplibregl.removeProtocol(attempt.protocol);
    attempt.container?.remove();
  }

  function checkedRequests(attempt) {
    const protocol = attempt.protocol = 'team-share-basemap-' + (++sequence);
    const prefix = protocol + '://';
    // A failed response can contain a valid picture, including a 403 notice.
    // Read HTTP status before allowing MapLibre to decode any image or tile.
    maplibregl.addProtocol(protocol, async (request, controller) => {
      const timeout = AbortSignal.timeout(deadlineMs);
      const signal = AbortSignal.any([attempt.controller.signal, controller.signal, timeout]);
      try {
        const response = await fetch('https://' + request.url.slice(prefix.length), {signal});
        if (!response.ok) {
          fail(attempt, '배경지도 HTTP ' + response.status + ' 오류입니다.');
          throw new Error('Basemap HTTP ' + response.status);
        }
        const data = request.type === 'json' ? await response.json() :
          request.type === 'string' ? await response.text() : await response.arrayBuffer();
        return {data};
      } catch (error) {
        if (timeout.aborted) fail(attempt, '배경지도 연결 시간이 초과되었습니다.');
        else if (!signal.aborted) fail(attempt, '배경지도 연결에 실패했습니다.');
        throw error;
      }
    });
    return url => ({
      url: providerOrigins.some(origin => url.startsWith(origin + '/')) ? prefix + url.slice(8) : url,
    });
  }

  function fail(attempt, reason) {
    if (session !== attempt) return;
    session = null;
    attempt.controller.abort();
    if (attempt.container) attempt.container.style.visibility = 'hidden';
    showOffline(true);
    updateControl('offline', reason + ' 내장 배경을 표시합니다.');
    // Finish the renderer's event handler before removing its request sources.
    queueMicrotask(() => release(attempt));
  }

  async function start() {
    const attempt = {controller:new AbortController()};
    session = attempt;
    updateControl('connecting', '온라인 배경지도에 연결 중입니다.');
    if (!navigator.onLine) {
      fail(attempt, '인터넷 연결이 없습니다.');
      return;
    }
    attempt.timeout = setTimeout(() => fail(attempt, '배경지도 연결 시간이 초과되었습니다.'), deadlineMs);
    try {
      await Promise.all(mapLibreAssets.map(asset => loadBasemapAsset(asset, attempt.controller.signal)));
      if (session !== attempt) return;
      const container = attempt.container = L.DomUtil.create('div', 'online-map', pane);
      container.style.visibility = 'hidden';
      const center = () => {
        const {lat,lng} = map.getCenter();
        return [lng, lat];
      };
      // Leaflet uses a 256-pixel world at zoom 0, while MapLibre uses 512 pixels.
      const zoom = () => map.getZoom() - 1;
      function syncViewport() {
        attempt.frame = 0;
        const size = map.getSize();
        const resized = container.clientWidth !== size.x || container.clientHeight !== size.y;
        container.style.width = size.x + 'px';
        container.style.height = size.y + 'px';
        L.DomUtil.setPosition(container, map.containerPointToLayerPoint([0,0]));
        if (resized) attempt.renderer?.resize();
        attempt.renderer?.jumpTo({center:center(), zoom:zoom()});
      }
      syncViewport();
      const renderer = attempt.renderer = new maplibregl.Map({
        container, style, center:center(), zoom:zoom(), interactive:false, attributionControl:false,
        renderWorldCopies:false, refreshExpiredTiles:false, trackResize:false,
        transformRequest:checkedRequests(attempt),
      });
      attempt.update = () => {
        if (!attempt.frame) attempt.frame = requestAnimationFrame(syncViewport);
      };
      map.on('move zoom resize', attempt.update);
      renderer.on('error', ({error}) => {
        const status = Number(error?.status);
        const reason = status >= 400 ? '배경지도 HTTP ' + status + ' 오류입니다.' : '배경지도 연결에 실패했습니다.';
        fail(attempt, reason);
      });
      renderer.on('webglcontextlost', () => fail(attempt, '배경지도 WebGL 연결이 끊겼습니다.'));
      renderer.once('load', () => {
        if (session !== attempt) return;
        clearTimeout(attempt.timeout);
        attempt.ready = true;
        map.attributionControl.addAttribution(credit);
        container.style.visibility = '';
        showOffline(false);
        updateControl('online');
      });
    } catch (error) {
      fail(attempt, attempt.container ? '온라인 배경지도를 초기화할 수 없습니다.' : error.message);
    }
  }

  button.addEventListener('click', () => {
    if (!session) {
      start();
      return;
    }
    const attempt = session;
    session = null;
    release(attempt);
    showOffline(false);
    updateControl('hidden');
  });
  window.addEventListener('offline', () => {
    if (session) fail(session, '인터넷 연결이 끊겼습니다.');
  });
  start();
}

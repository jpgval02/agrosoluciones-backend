// Sistema ASOA - Service Worker
// Guarda una copia de la página en el dispositivo para que pueda abrir sin internet.
// Sube este número cada vez que quieras forzar que los celulares bajen la versión nueva.
const VERSION_CACHE = 'asoa-cache-v2';

const ARCHIVOS_A_GUARDAR = [
    './index.html',
    './manifest.json'
    // Los recursos externos (Tailwind, Font Awesome, ApexCharts, html2pdf) NO se
    // precargan aquí porque el navegador bloquea por CORS el intento de guardarlos
    // en bloque al instalar. En su lugar, el "fetch" de abajo los va guardando
    // solos la primera vez que se cargan normalmente (con internet), y de ahí
    // en adelante ya quedan disponibles sin internet también.
];

// Se ejecuta cuando se instala el Service Worker (primera vez que alguien entra con internet)
self.addEventListener('install', (evento) => {
    evento.waitUntil(
        caches.open(VERSION_CACHE).then((cache) => {
            return cache.addAll(ARCHIVOS_A_GUARDAR);
        })
    );
    self.skipWaiting();
});

// Borra cachés viejos cuando se activa una versión nueva del Service Worker
self.addEventListener('activate', (evento) => {
    evento.waitUntil(
        caches.keys().then((nombres) => {
            return Promise.all(
                nombres.filter((nombre) => nombre !== VERSION_CACHE)
                       .map((nombre) => caches.delete(nombre))
            );
        })
    );
    self.clients.claim();
});

// Intercepta las peticiones: intenta traer de internet primero (para que siempre
// se vea la versión más nueva si hay señal); si falla (sin internet), usa la copia guardada.
self.addEventListener('fetch', (evento) => {
    // Las llamadas a tu API (Render) SIEMPRE deben ir a internet, nunca a caché,
    // porque son datos en vivo (clientes, cotizaciones, servicios, etc.)
    if (evento.request.url.includes('agrosoluciones-backend.onrender.com')) {
        return; // deja que el navegador la maneje normal, sin intervenir
    }

    evento.respondWith(
        fetch(evento.request)
            .then((respuestaRed) => {
                // Si hay internet, guarda una copia fresca para la próxima vez sin señal
                const copia = respuestaRed.clone();
                caches.open(VERSION_CACHE).then((cache) => {
                    cache.put(evento.request, copia).catch(() => {
                        // Algunos recursos de otros dominios no se pueden guardar; no pasa nada, se ignora.
                    });
                });
                return respuestaRed;
            })
            .catch(() => {
                // Sin internet: usa lo que ya tengamos guardado
                return caches.match(evento.request).then((respuestaCache) => {
                    return respuestaCache || caches.match('./index.html');
                });
            })
    );
});
// Sistema ASOA - Service Worker
// Guarda una copia de la página en el dispositivo para que pueda abrir sin internet.
// Sube este número cada vez que quieras forzar que los celulares bajen la versión nueva.
const VERSION_CACHE = 'asoa-cache-v1';

const ARCHIVOS_A_GUARDAR = [
    './index.html',
    './manifest.json',
    'https://cdn.tailwindcss.com',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css',
    'https://cdn.jsdelivr.net/npm/apexcharts',
    'https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js'
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
                caches.open(VERSION_CACHE).then((cache) => cache.put(evento.request, copia));
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
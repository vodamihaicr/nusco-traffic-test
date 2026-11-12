# Test trafic Nusco City faza 2 vs U Home

Măsoară la fiecare 30 de minute, timp de 8 zile, timpul de condus în timp real (Google Maps)
din Nusco City faza 2 și U Home către 8 destinații, în ambele sensuri. Rezultatele se adaugă în `data/measurements.csv`.

## Setup (o singură dată)
1. **Cheie Google Maps**: Google Cloud Console → proiect nou → activează *Routes API* și *Geocoding API*
   → Credentials → Create API key (restricționează-o la cele două API-uri). Contul trebuie să aibă billing activ.
2. **Secret în GitHub**: repository → Settings → Secrets and variables → Actions → New repository secret,
   nume `GOOGLE_MAPS_API_KEY`, valoarea = cheia.
3. **Pornire**: tab-ul Actions → *traffic-test* → *Run workflow*. Apoi rulează singur la fiecare 30 de minute
   și se oprește după 8 zile de la prima rulare.

## Coloane CSV
`local_time` (ora României), `weekday`, `hour_slot`, `route`, `direction`, `google_min` (cu trafic),
`google_no_traffic_min`, `google_km`, `errors`.

## Observații
- GitHub poate întârzia rulările programate cu câteva minute la ore aglomerate; ora reală e salvată în fiecare rând.

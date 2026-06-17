# Arb Bileti — Polymarket Günlük Arbitraj Taraması

Her sabah otomatik çalışır: Polymarket'taki **açık, deadline'ı 14 gün içinde olan**
"çok seçenekli" (negRisk) event'leri tarar, seçeneklerin toplam satış fiyatı
$1.00'ın altına düştüğü — yani teorik olarak risksiz bir kâr payı bırakan —
durumları bulur ve bunları bilet kartları halinde bir dashboard'da listeler.

**Maliyet: $0. Sonsuza kadar.** GitHub Actions (cron) + GitHub Pages (statik
barındırma) kullanıyor — ne sunucu var, ne de uyanık tutman gereken bir şey.

## Nasıl çalışıyor

```
GitHub Actions (her sabah 06:00 TR saati)
   └─ scripts/fetch_arbitrage.py  → Polymarket Gamma API'sini tarar
        └─ docs/results.json'ı yazar ve repo'ya commit'ler
              └─ GitHub Pages docs/index.html bu json'ı okuyup bilet kartlarını çizer
```

Hesaplama mantığı `scripts/fetch_arbitrage.py` dosyasının başında Türkçe
yorumlarla anlatılıyor. Kısacası: bir event'te birbirini dışlayan N seçenek
varsa (örn. "Bu yarışı kim kazanır?") ve hepsinin "Yes" tarafını şu anki en
iyi satış fiyatından alıp toplarsan toplam $1.00'ın altında kalıyorsa, bu
bilet olarak listelenir.

## Kurulum (5 dakika)

1. **Yeni bir GitHub reposu oluştur** (public — Actions dakikaları public
   repolarda ücretsiz ve sınırsız; private de istersen çalışır ama aylık
   dakika kotası var).
2. Bu klasördeki her şeyi o reponun köküne kopyala ve push'la:
   ```
   git add .
   git commit -m "ilk kurulum"
   git push
   ```
3. **GitHub Pages'i aç:** Repo → Settings → Pages → "Build and deployment"
   altında Source: *Deploy from a branch*, Branch: `main`, klasör: `/docs`.
   Kaydet. Birkaç dakika içinde `https://kullanici-adin.github.io/repo-adi/`
   adresinde yayında olacak.
4. **İlk taramayı manuel tetikle:** Repo → Actions → "Daily Polymarket Scan" →
   "Run workflow". Bu, ilk `results.json`'ı üretip commit'leyecek. Bundan
   sonra her sabah otomatik çalışacak.

## Ayarlanabilir parametreler

`scripts/fetch_arbitrage.py` dosyasının üstünde:

| Parametre | Ne işe yarar | Varsayılan |
|---|---|---|
| `DAYS_AHEAD` | Deadline'ı en fazla kaç gün sonra olan event'ler taransın | 14 |
| `MIN_EDGE_PCT` | Bu yüzdenin altındaki "gürültü" seviyesindeki farklar gösterilmesin | 0.5 |
| `MIN_LIQUIDITY_USD` | Her bacakta en az bu kadar likidite olsun (ince kitapları ele) | 50 |

Çalışma saatini değiştirmek için `.github/workflows/daily-scan.yml` içindeki
`cron: "0 3 * * *"` satırını düzenle (UTC saat kullanır; 3 = 06:00 TR saati).

## Önemli sınırlamalar — lütfen oku

- **Bu bir tarama/izleme aracı, para basma makinesi değil.** Gösterilen fark
  o anki order book "ask" fiyatlarından hesaplanan teorik bir farktır.
  Gerçekten işlem yapmaya kalktığında: likidite o tutarı karşılamayabilir,
  fiyat sen emri girene kadar değişmiş olabilir (slippage), ve/veya
  Polymarket ileride işlem ücreti uygulayabilir.
  Likit ve popüler event'lerde bu tür farklar genelde bot'lar tarafından
  saniyeler/dakikalar içinde kapatılır; bu aracın gerçekçi kullanım alanı
  daha az likit/uzun kuyruktaki event'lerde oluşan kısa ömürlü fırsatları
  yakalamak ya da genel piyasa takibi.
- Şu an sadece **çok seçenekli (negRisk) event grupları** taranıyor. Klasik
  tek soru-cevap (Yes/No) marketlerde NO tarafının gerçek satış fiyatı
  Gamma API'de ayrı bir alan olarak gelmiyor; CLOB order book endpoint'i de
  bilinen bir "durağan/yanlış veri" sorunu taşıdığı için bilerek dışarıda
  tutuldu. İstersen ileride bunu da ekleyebiliriz.
- Bu araç **yatırım tavsiyesi vermez**, sadece halka açık piyasa verisini
  işler. Ben (Claude) finansal danışman değilim.
- Script, ağ erişimi kısıtlı bir ortamda yazıldığı için canlı API'ye karşı
  uçtan uca test edilmedi — gerçek API yanıt şekli web üzerinden doğrulandı
  ama ilk çalıştırmayı (adım 4) takip etmen iyi olur.

## Lisans / sorumluluk

Kişisel kullanım için. Gerçek parayla işlem yapmadan önce kendi araştırmanı
yap.

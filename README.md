# verifica-rca

![verifica-rca: Italian number plate, RCA, classe ambientale and neopatentati](assets/thumbnail.png)

[![GitHub source](https://img.shields.io/badge/GitHub-Source-181717?logo=github&logoColor=white)](https://github.com/Jwjfr2/verifica-rca)
[![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)](rca.py)
[![macOS with Apple Vision](https://img.shields.io/badge/macOS-Apple_Vision-555555?logo=apple&logoColor=white)](ocr_vision.swift)

Python client for checking Italian vehicle insurance, emissions class and
neopatentati eligibility on the
[Portale dell'Automobilista](https://www.ilportaledellautomobilista.it).
Captcha recognition runs locally with Apple Vision.

Requires macOS, Python, [uv](https://docs.astral.sh/uv/) and Xcode Command Line Tools.

## Setup

```sh
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
python vision_ocr.py --build
```

The last command compiles the Swift OCR helper into `build/vision_ocr`.
It also builds automatically on first use.

## Usage

```sh
python rca.py GA123BC
```

Results print on one line, with the portal's original Italian field names and messages.

| Service | Results |
| --- | --- |
| RCA | Insurer, coverage expiry and insurance status |
| Classe ambientale | Euro emissions class and CO2 g/km |
| Neopatentati | Whether a newly licensed driver may drive the car |

Cars use `--tipo A`, the default. For other vehicles:

```sh
python rca.py --tipo M AB12345       # motorcycle
python rca.py --tipo R AB12345       # trailer
python rca.py --cic 1234567          # moped, using its CIC number
```

Neopatentati checks apply only to cars. A CIC-only lookup checks RCA only.
Revision history and licence points are not available through these anonymous forms.

### Multiple plates

```sh
python batch.py GA123BC GX999ZZ
python batch.py < plates.txt
```

Use one plate per line in `plates.txt`. The batch client reuses sessions between lookups.

### Proxies

```sh
python rca.py --proxy http://user:pass@host:port GA123BC
python batch.py --proxy-file proxies.txt < plates.txt
```

Proxy files take one URL per line. The client tries the next proxy after a
connection failure or WAF block. Each captcha solve stays on the same proxy.

The portal rate-limits requests. Keep request volume low and don't rotate proxies
to bypass a ban.

## OCR and limitations

Captchas are 150 x 50 images containing four digits. Grid backgrounds and black
text on black often make them unreadable. The client fetches another image when
it can't read one, stopping after `--max-captchas` attempts (default: 15).

On the 20 labelled samples: 16 correct, 4 unreadable, no wrong readings.
Median OCR time was about 23 ms per image. Results may differ on new captchas.
Run the local evaluation:

```sh
python evaluate.py
```

`dataset/` contains 53 captured images. `labels.json` covers the hand-labelled subset.
`bench_vision.py` compares OCR scales and recognition levels.

<details>
<summary>Notes on the portal and captcha</summary>

The three services are Liferay portlets using the same captcha generator. The
client reads each page's form action, field names and captcha URL. RCA requires
a vehicle/moped selection first. The other two services show their forms directly.

Challenges belong to a `JSESSIONID`. Fetching another image replaces the current
challenge. A wrong answer leaves it valid; a successful answer consumes it.
The three services run concurrently with separate sessions.

The Swift helper scales images to twice their size and uses `VNRecognizeTextRequest`
in a persistent process. It discards readings below the confidence threshold.
Preprocessing and extra OCR hypotheses did not help with grid images in the
original experiments.

Rapid submissions triggered a temporary IP block during testing.

</details>

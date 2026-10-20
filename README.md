# AXIOM Pricing Module

**Turns a company's own sales history into clear pricing answers:** where money is lost between the price list and the bank account, which customers behave how, and what price to quote next.

Built by Nourjihen Elabed during an internship at NIOUS GmbH (2026), as the pricing engine ("Track A") of the AXIOM Pricing Module.

---

## What it does ?

1. **Reads the sales data.** Orders, invoices, discounts, rebates, costs, and quotes that were won or lost.
2. **Checks it before using it.** If the data contradicts itself, the engine stops and says why, instead of producing wrong numbers.
3. **Follows the money.** For every order, it traces the path from the list price, through every discount and rebate, down to what the company actually keeps (the margin). This can be broken down by customer, product, channel, region or month.
4. **Learns how customers buy.** It groups customers by behaviour (for example "Loyal payers" or "Big-volume hagglers"), measures how strongly each group reacts to a price change, and learns from past quotes how the chance of winning changes with price.
5. **Recommends prices.** For a customer and a product, it suggests a price range with the best expected profit, while respecting rules you set: never below cost plus a margin, never above list price, keep a minimum chance of winning, and do not lose too much volume.

Every number can be traced back to the orders and quotes it came from. When there is not enough data to answer, the engine says so rather than guessing.

---

## See it in two minutes: the demo website

The demo is a small website that shows everything above on a realistic sample dataset (3,724 orders, 560 customers, 30 months).

| Page | What you see |
|---|---|
| **How it works** | The five steps above, and why the results can be trusted |
| **Overview** | "Of every 100 SAR on the price list, you keep 22": where the money goes, month by month |
| **Margin explorer** | Margin by channel, region, customer type, industry or product, plus a channel × region map |
| **Customers** | The customer groups, a map of every customer by revenue and margin, and a profile page for each |
| **Price advisor** | The recommended price for any customer and product, why, and a slider to try your own price |
| **Opportunity** | What the recommended prices would be worth per year |

Filters at the top (channel, region, period and more) update every page at once. Clicking a bar or a dot does the same.

How to start it is explained in the next section.

---

## How to run it on your computer

You need **Python 3.10** and the sample data folder `data/snapshot_axiom_v2` (the data is not stored on GitHub; ask the project owner for it and put it in the `data` folder).

**1. Install** (once):

```bash
python -m venv axiompricingvenv
axiompricingvenv\Scripts\activate
pip install -r requirements.txt
```

On Mac or Linux, the second line is `source axiompricingvenv/bin/activate`.

**2. Start the demo website:**

```bash
python demo/server.py --cache
```

Then open **http://127.0.0.1:8765** in a browser. The first start takes about a minute while the engine does its calculations; after that it opens in seconds.

**3. Or run the full calculation and save every result as files:**

```bash
python -m dvc repro
```

This re-creates every result from the data in one command, and writes them to the `reports_axiom_v2` folder. The most important file is `engine_result.json`: it is what the second part of the project (the narrative layer, "Track B") reads to explain the numbers in words.

**4. Check that everything works:**

```bash
pytest -q
```

---

## Running it with Docker

Docker packages the engine and everything it needs into one ready-to-run box (an "image"), so it works the same on any computer.

**You do not need to build it yourself.** Every time code is pushed to GitHub, GitHub builds the image on its own servers, checks that the engine and the demo start inside it, and publishes it. Next to each commit on GitHub you will see a green tick (it worked) or a red cross (something broke); the details are under the **Actions** tab, and the published image under **Packages**.

The image is **public**: anyone can download it, no GitHub account or login needed.

**What you need:** Docker installed ([Docker Desktop](https://www.docker.com/products/docker-desktop/) on Windows or Mac), and the `data` folder from the project owner. The data is not on GitHub and not inside the image; you give it to the image when you start it.

**1. Download the image** (once, and again whenever you want the latest version):

```bash
docker pull ghcr.io/nourjihenelabed/nious:latest
```

**2. Start the demo website.** Run this in the folder that contains your `data` folder:

```bash
docker run --rm -p 8765:8765 -v "$PWD/data:/app/data" ghcr.io/nourjihenelabed/nious:latest demo/server.py --host 0.0.0.0
```

Then open **http://localhost:8765**. The first start takes about a minute.

What the parts of that command mean:

| Part | Meaning |
|---|---|
| `--rm` | Clean up the box when you stop it |
| `-p 8765:8765` | Make the website reachable on your computer at port 8765 |
| `-v "$PWD/data:/app/data"` | Give the box access to your `data` folder |
| `demo/server.py --host 0.0.0.0` | What to run inside the box: the demo website |

On Windows, run these commands in PowerShell, where they work as written. In the older Command Prompt, replace `$PWD` with `%cd%`.

**3. Or run the calculations instead of the website,** and save the results in a `reports_axiom_v2` folder next to your data:

```bash
docker run --rm -v "$PWD/data:/app/data" -v "$PWD/reports_axiom_v2:/app/reports_axiom_v2" ghcr.io/nourjihenelabed/nious:latest scripts/run_models.py --profile axiom_v2 --graph data/snapshot_axiom_v2 --out reports_axiom_v2
```

To stop the website, press `Ctrl + C` in the terminal.

---

## Using your own data

The engine is not tied to one company. Each data source is described once in a small settings file (a "profile") that says which column means what. Three are included: a synthetic distributor, a dairy company model, and the AXIOM knowledge graph. Before running on new data, this command tells you whether the data fits and what is missing:

```bash
python scripts/check_data.py --describe
```

---

## What is in this folder

| Folder | Contents |
|---|---|
| `src/pricing/` | The engine itself |
| `scripts/` | Commands that run each step |
| `demo/` | The demo website |
| `tests/` | 250 automatic checks that the numbers are right |
| `docs/` | Detailed write-ups (see below) |
| `notebooks/` | Data exploration |

**Further reading**

- [`docs/CHAIN.md`](docs/CHAIN.md): how each step feeds the next, with the results on the sample data
- [`docs/TECHNICAL.md`](docs/TECHNICAL.md): the technical guide (setup options, every command, data sources)
- [`docs/CONSTANTS.md`](docs/CONSTANTS.md): every fixed number in the engine and where it comes from
- [`REPORT.md`](REPORT.md): the full project report

---

## Project status

All eight planned deliverables are built and tested: data access, the price breakdown, margin analysis with a data-quality score, customer groups, price sensitivity, win probability with price recommendations, the tracking and versioning set-up, and the result file for the narrative layer.

Still open: the result file format is marked as a draft (version 0.2.0) until it is agreed with Track B, and the end-to-end test with the Track B narrative layer has not been run yet.

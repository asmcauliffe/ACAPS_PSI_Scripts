const DRIVERS = ["Conflict", "Political", "Flood", "Drought", "Earthquake", "Cyclone", "Displacement"];

const STORAGE = {
  pre_split: "ACLED and UCDP are one score, already split 56.25 / 43.75.",
  acled_holds_full_score: "UCDP is zero everywhere and ACLED holds the full conflict score.",
  independent: "ACLED and UCDP are separate series. Weight them once.",
};

function score(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(3);
}

function people(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Math.round(Number(value)).toLocaleString("en-GB");
}

async function loadJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(path);
  return response.json();
}

function params() {
  return new URLSearchParams(window.location.search);
}

document.addEventListener("DOMContentLoaded", () => {
  const explorer = document.querySelector("[data-explorer]");
  if (explorer) initExplorer(explorer);
  const national = document.querySelector("[data-national]");
  if (national) initNational(national);
  const dictionary = document.querySelector("[data-dictionary]");
  if (dictionary) initDictionary(dictionary);
  const examples = document.querySelector("[data-examples]");
  if (examples) initExamples(examples);
});

async function initExplorer(root) {
  const status = root.querySelector("[data-status]");
  try {
    const [national, districts] = await Promise.all([
      loadJson("data/national.json"),
      loadJson("data/districts.json"),
    ]);
    const rows = national.rows;
    const countries = [...new Set(rows.map((row) => row.country))];
    const query = params();
    const state = {
      country: query.get("country") || "Afghanistan",
      period: query.get("period") || "July 2025",
      q: "",
      page: 0,
      source: false,
      pageSize: 20,
    };
    if (!countries.includes(state.country)) state.country = countries[0];

    const countrySelect = root.querySelector("[data-country]");
    const periodSelect = root.querySelector("[data-period]");
    const search = root.querySelector("[data-search]");
    const source = root.querySelector("[data-source]");
    countrySelect.innerHTML = countries
      .map((country) => `<option value="${country}">${country}</option>`)
      .join("");
    countrySelect.value = state.country;

    function periodsFor(country) {
      return rows
        .filter((row) => row.country === country)
        .sort((a, b) => a.period_start.localeCompare(b.period_start));
    }

    function fillPeriods() {
      const periods = periodsFor(state.country);
      periodSelect.innerHTML = periods
        .map((row) => `<option value="${row.period}">${row.period}</option>`)
        .join("");
      if (!periods.some((row) => row.period === state.period)) {
        state.period = periods[periods.length - 1].period;
      }
      periodSelect.value = state.period;
    }

    function current() {
      return rows.find((row) => row.country === state.country && row.period === state.period);
    }

    function render() {
      fillPeriods();
      const row = current();
      const airtable = national.published_iran_airtable?.[row.period];
      const showAirtable = row.country === "Iran" && airtable !== undefined;
      root.querySelector("[data-scores]").innerHTML = [
        card("Dashboard simulation", row.psi_dashboard, "What the Shiny app shows at its default weights. It is a simulator, not a second official estimate."),
        card("Reconstructed", row.psi_reconstructed, "Conflict counted once, severity scales, IOM alone, missing drivers dropped. Cyclone stays in as zero.", "lead"),
        card(
          "Without cyclone",
          row.psi_without_cyclone,
          showAirtable
            ? `Airtable published ${airtable.toFixed(2)} for this month. The gap is under 0.03.`
            : "Same reconstruction with the structural cyclone zero left out of the mean.",
          showAirtable ? "match" : ""
        ),
      ].join("");

      root.querySelector("[data-meta]").textContent =
        `${people(row.population)} people across ${row.adm2_count} districts. ${STORAGE[row.conflict_storage] || ""}`;

      root.querySelector("[data-drivers]").innerHTML = DRIVERS.map((name) => {
        const dash = row.drivers_dashboard[name];
        const recon = row.drivers_reconstructed[name];
        return `<div class="driver">
          <div class="name">${name}</div>
          <div class="bars">
            ${bar(dash, "dash")}
            ${recon === null ? `<div class="bar"><span style="width:0"></span></div>` : bar(recon, "ink")}
          </div>
          <div class="num">${score(dash)}<br>${recon === null ? "left out" : score(recon)}</div>
        </div>`;
      }).join("");

      root.querySelector("[data-pins]").innerHTML = [
        pin(people(row.pin_conflict_dashboard), "Dashboard conflict PiN", `${row.districts_conflict_pin_dashboard} districts where either stored column is at least 3.3.`),
        pin(people(row.pin_conflict_district_reconstructed), "District conflict PiN", `${row.districts_conflict_pin_reconstructed} districts where the reconstructed score is at least 3.3. Still not the pixel PiN.`),
        pin(people(row.pin_displacement), "Displacement PiN", "Raw IOM IDP count. The same number in both readings."),
      ].join("");

      root.querySelector("[data-notes]").innerHTML = row.notes.map((note) => `<li>${note}</li>`).join("");

      const filtered = districts.filter((district) => {
        if (district.country !== state.country || district.period !== state.period) return false;
        if (!state.q) return true;
        const hay = `${district.admin1_name} ${district.admin2_name} ${district.adm2_id}`.toLowerCase();
        return hay.includes(state.q);
      });
      const pages = Math.max(1, Math.ceil(filtered.length / state.pageSize));
      if (state.page >= pages) state.page = pages - 1;
      const start = state.page * state.pageSize;
      const slice = filtered.slice(start, start + state.pageSize);
      root.querySelector("[data-table]").innerHTML = tableHtml(slice, state.source, filtered.length === 0);
      const pager = root.querySelector("[data-pager]");
      pager.querySelector("[data-range]").textContent = filtered.length
        ? `${start + 1}–${start + slice.length} of ${filtered.length.toLocaleString("en-GB")}`
        : "0 districts";
      pager.querySelector("[data-prev]").disabled = state.page === 0;
      pager.querySelector("[data-next]").disabled = state.page >= pages - 1;

      const url = new URL(window.location.href);
      url.searchParams.set("country", state.country);
      url.searchParams.set("period", state.period);
      history.replaceState(null, "", url);
    }

    countrySelect.addEventListener("change", () => {
      state.country = countrySelect.value;
      state.page = 0;
      render();
    });
    periodSelect.addEventListener("change", () => {
      state.period = periodSelect.value;
      state.page = 0;
      render();
    });
    search.addEventListener("input", () => {
      state.q = search.value.trim().toLowerCase();
      state.page = 0;
      render();
    });
    source.addEventListener("change", () => {
      state.source = source.checked;
      render();
    });
    root.querySelector("[data-prev]").addEventListener("click", () => {
      state.page -= 1;
      render();
    });
    root.querySelector("[data-next]").addEventListener("click", () => {
      state.page += 1;
      render();
    });

    status.hidden = true;
    render();
  } catch (error) {
    status.textContent = "The tables did not load. Open this site over HTTP — GitHub Pages does that — rather than as a file on disk.";
    console.error(error);
  }
}

function card(label, value, why, extra) {
  return `<article class="score ${extra || ""}">
    <p class="label">${label}</p>
    <p class="value">${score(value)}</p>
    <p class="why">${why}</p>
  </article>`;
}

function bar(value, kind) {
  const width = Math.max(0, Math.min(100, (Number(value) / 10) * 100));
  return `<div class="bar ${kind}"><span style="width:${width}%"></span></div>`;
}

function pin(num, title, detail) {
  return `<div class="pin"><div class="num">${num}</div><p><strong>${title}.</strong> ${detail}</p></div>`;
}

function tableHtml(rows, source, empty) {
  if (empty) {
    return `<p class="empty" style="padding:1rem">No district matches that search.</p>`;
  }
  const extraHead = source
    ? `<th class="num">ACLED</th><th class="num">UCDP</th><th>Rule</th><th class="num">IOM</th><th>IOM status</th>`
    : "";
  const body = rows.map((row) => {
    const extra = source
      ? `<td class="num">${score(row.acled_conflict_adm2_sev)}</td>
         <td class="num">${score(row.ucdp_conflict_adm2_sev)}</td>
         <td>${row.conflict_column_rule}</td>
         <td class="num">${score(row.iom_severity)}</td>
         <td>${row.iom_status}</td>`
      : "";
    const note = row.acled_protests_note ? ` title="${row.acled_protests_note}"` : "";
    return `<tr${note}>
      <td>${row.admin1_name}</td>
      <td>${row.admin2_name}</td>
      <td class="num">${people(row.population)}</td>
      <td class="num">${score(row.conflict_dashboard)}</td>
      <td class="num">${score(row.conflict_reconstructed)}</td>
      <td class="num">${score(row.displacement_dashboard)}</td>
      <td class="num">${score(row.displacement_reconstructed)}</td>
      <td class="num">${people(row.pin_conflict_dashboard)}</td>
      <td class="num">${people(row.pin_conflict_district_reconstructed)}</td>
      ${extra}
    </tr>`;
  }).join("");
  return `<div class="table-wrap"><table>
    <thead><tr>
      <th>Admin 1</th><th>Admin 2</th><th class="num">Population</th>
      <th class="num">Conflict, dashboard</th><th class="num">Conflict, reconstructed</th>
      <th class="num">Displacement, dashboard</th><th class="num">Displacement, IOM</th>
      <th class="num">Conflict PiN, dashboard</th><th class="num">Conflict PiN, district</th>
      ${extraHead}
    </tr></thead>
    <tbody>${body}</tbody>
  </table></div>`;
}

async function initNational(root) {
  try {
    const national = await loadJson("data/national.json");
    const body = national.rows.map((row) => `<tr>
      <td><a href="index.html?country=${encodeURIComponent(row.country)}&period=${encodeURIComponent(row.period)}">${row.country}</a></td>
      <td>${row.period}</td>
      <td class="num">${score(row.psi_dashboard)}</td>
      <td class="num">${score(row.psi_reconstructed)}</td>
      <td class="num">${score(row.psi_without_cyclone)}</td>
      <td class="num">${people(row.pin_conflict_dashboard)}</td>
      <td class="num">${people(row.pin_conflict_district_reconstructed)}</td>
      <td>${row.conflict_storage.replaceAll("_", " ")}</td>
    </tr>`).join("");
    root.innerHTML = `<div class="table-wrap"><table>
      <thead><tr>
        <th>Country</th><th>Month</th>
        <th class="num">Dashboard</th><th class="num">Reconstructed</th><th class="num">Without cyclone</th>
        <th class="num">Conflict PiN, dashboard</th><th class="num">Conflict PiN, district</th>
        <th>Conflict storage</th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table></div>`;
  } catch (error) {
    root.textContent = "The national table did not load.";
    console.error(error);
  }
}

async function initDictionary(root) {
  try {
    const text = await (await fetch("data/psi_data_dictionary.csv")).text();
    const lines = text.trim().split(/\r?\n/).slice(1);
    const rows = lines.map((line) => {
      const comma = line.indexOf(",");
      const column = line.slice(0, comma);
      let meaning = line.slice(comma + 1);
      if (meaning.startsWith('"') && meaning.endsWith('"')) meaning = meaning.slice(1, -1).replaceAll('""', '"');
      return `<tr><td><code>${column}</code></td><td>${meaning}</td></tr>`;
    }).join("");
    root.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Column</th><th>Meaning</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
  } catch (error) {
    root.textContent = "The data dictionary did not load.";
    console.error(error);
  }
}

async function initExamples(root) {
  try {
    const examples = await loadJson("data/examples.json");
    const dand = examples.dand_wa_patan;
    const july = examples.kabul_july;
    const august = examples.kabul_august;
    const majang = examples.majang;
    root.innerHTML = `
      <p><strong>${dand.admin2}, ${dand.admin1}, ${dand.period}.</strong>
      ACLED ${score(dand.acled)} and UCDP ${score(dand.ucdp)} sum to ${score(dand.combined)}.
      The dashboard weights that pair again and gets ${score(dand.dashboard)}.</p>
      <p><strong>Kabul and Bagrami, July 2025.</strong>
      Kabul’s pieces are ${score(july.kabul_acled)} and ${score(july.kabul_ucdp)}, combined ${score(july.kabul_combined)}, population ${people(july.kabul_population)}.
      Bagrami combined ${score(july.bagrami_combined)}, population ${people(july.bagrami_population)}.
      Dashboard conflict PiN is ${people(july.pin_dashboard)}. The district rule counts both and reaches ${people(july.pin_district)}.</p>
      <p><strong>August 2025.</strong>
      Kabul combined ${score(august.kabul_combined)}, Bagrami combined ${score(august.bagrami_combined)}.
      Each stored piece is still under 3.3, so the dashboard conflict PiN is ${people(august.pin_dashboard)} again.
      The same two districts sum to ${people(august.pin_district)}.</p>
      <p><strong>${majang.admin2}, ${majang.admin1}, ${majang.period}.</strong>
      Protest severity is ${score(majang.protests)} in the source file. The raw value is kept. The reconstructed political score treats it as zero.</p>`;
  } catch (error) {
    root.textContent = "The worked examples did not load.";
    console.error(error);
  }
}

# =============================================================================
# GDACS Pipeline — Fetch, Spatial Join, Monthly Panel
# Events: Earthquakes (EQ) & Tropical Cyclones (TC)
# Date range: 2018-01-01 to present
# =============================================================================
# Required packages:
#   install.packages(c("httr", "jsonlite", "dplyr", "sf", "tidyr"))
# =============================================================================

library(httr)
library(jsonlite)
library(dplyr)
library(sf)
library(tidyr)

# =============================================================================
# PART 1: FETCH FROM GDACS API
# =============================================================================

BASE_URL     <- "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
EVENT_TYPES  <- "EQ;TC"
ALERT_LEVELS <- "green;orange;red"
START_DATE   <- as.Date("2018-01-01")
END_DATE     <- Sys.Date()
CHUNK_MONTHS <- 12  # fetch one year at a time to avoid record caps

# -----------------------------------------------------------------------------
# Helper: fetch one date window, return an sf object (or NULL on failure)
# -----------------------------------------------------------------------------
fetch_window <- function(from_date, to_date) {
  params <- list(
    eventlist  = EVENT_TYPES,
    fromdate   = format(from_date, "%Y-%m-%d"),
    todate     = format(to_date,   "%Y-%m-%d"),
    alertlevel = ALERT_LEVELS
  )
  
  message(sprintf("  Fetching %s → %s ...", params$fromdate, params$todate))
  
  resp <- tryCatch(
    GET(BASE_URL, query = params, timeout(60),
        add_headers(Accept = "application/json")),
    error = function(e) { warning(sprintf("Request failed: %s", e$message)); NULL }
  )
  
  if (is.null(resp) || http_error(resp)) {
    warning(sprintf("HTTP error for window %s → %s", params$fromdate, params$todate))
    return(NULL)
  }
  
  geojson_text <- content(resp, as = "text", encoding = "UTF-8")
  
  sf_obj <- tryCatch(
    st_read(geojson_text, quiet = TRUE),
    error = function(e) { warning(sprintf("st_read failed: %s", e$message)); NULL }
  )
  
  if (is.null(sf_obj) || nrow(sf_obj) == 0) {
    message("    No events in this window.")
    return(NULL)
  }
  
  message(sprintf("    Retrieved %d events.", nrow(sf_obj)))
  sf_obj
}

# -----------------------------------------------------------------------------
# Build annual date windows
# -----------------------------------------------------------------------------
build_windows <- function(start, end, months = CHUNK_MONTHS) {
  windows <- list()
  current <- start
  while (current <= end) {
    next_date <- min(
      seq(current, by = paste(months, "months"), length.out = 2)[2] - 1,
      end
    )
    windows <- c(windows, list(list(from = current, to = next_date)))
    current <- next_date + 1
  }
  windows
}

# -----------------------------------------------------------------------------
# Fetch all windows
# -----------------------------------------------------------------------------
message("=== GDACS Event Fetcher ===")
message(sprintf("Event types : %s", EVENT_TYPES))
message(sprintf("Alert levels: %s", ALERT_LEVELS))
message(sprintf("Date range  : %s to %s\n", START_DATE, END_DATE))

windows    <- build_windows(START_DATE, END_DATE)
all_events <- lapply(windows, function(w) fetch_window(w$from, w$to))
events_sf  <- bind_rows(Filter(Negate(is.null), all_events))

if (nrow(events_sf) == 0) stop("No events retrieved. Check network or API.")

if ("eventid" %in% names(events_sf)) {
  before     <- nrow(events_sf)
  events_sf  <- events_sf[!duplicated(events_sf$eventid), ]
  message(sprintf("Deduplication removed %d rows.", before - nrow(events_sf)))
}

message(sprintf("✔ Total events fetched: %d\n", nrow(events_sf)))

# =============================================================================
# PART 2: LOAD ADMIN BOUNDARIES & CLEAN
# =============================================================================

boundaries_path <- file.path(
  "country_adm2_shapefiles",
  "countries_of_interest.geojson"
)
if (!file.exists(boundaries_path)) {
  stop(
    "Missing ", boundaries_path, ".\n",
    "  Run from repo root: Rscript country_adm2_shapefiles/shapefiles_combination.R"
  )
}
test <- st_read(boundaries_path, quiet = TRUE)

gdacs_clean <- events_sf |>
  mutate(
    date       = as.Date(fromdate),
    event_type = eventtype
  ) |>
  select(date, event_type, iso3, geometry)

# Harmonise country names in admin boundaries
gdacs_name_map <- c(
  "Iran (Islamic Republic of)" = "Iran",
  "Moçambique"                 = "Mozambique"
)

test <- test |>
  mutate(adm0_name = recode(adm0_name, !!!gdacs_name_map))

country_iso3 <- c(
  "Afghanistan" = "AFG",
  "Bangladesh"  = "BGD",
  "Cuba"        = "CUB",
  "Ethiopia"    = "ETH",
  "Guatemala"   = "GTM",
  "Iran"        = "IRN",
  "Kenya"       = "KEN",
  "Lebanon"     = "LBN",
  "Myanmar"     = "MMR",
  "Mozambique"  = "MOZ",
  "Malawi"      = "MWI",
  "South Sudan" = "SSD",
  "Yemen"       = "YEM"
)

test <- test |>
  mutate(iso3 = recode(adm0_name, !!!country_iso3))

# =============================================================================
# PART 3: SPATIAL JOIN
# =============================================================================

spatial_join_buffered <- function(admin, events, event_type_filter, buffer_m) {
  events_filtered <- events |>
    filter(event_type == event_type_filter) |>
    st_transform("+proj=moll") |>
    st_buffer(buffer_m) |>
    st_transform(st_crs(admin))
  
  st_join(admin, events_filtered, join = st_intersects, left = FALSE) |>
    st_drop_geometry()
}

sf_use_s2(FALSE)

result_eq <- spatial_join_buffered(test, gdacs_clean, "EQ", buffer_m = 100000)
result_tc <- spatial_join_buffered(test, gdacs_clean, "TC", buffer_m = 300000)

result_spatial <- bind_rows(result_eq, result_tc) |>
  mutate(disaster = 1L) |>
  distinct(adm2_pcode, adm2_name, adm0_name, adm1_name,
           date, event_type, disaster)

# =============================================================================
# PART 4: BUILD MONTHLY PANEL
# =============================================================================

# Aggregate spatial results to month level
result_monthly <- result_spatial |>
  mutate(year_month = format(date, "%Y-%m")) |>
  group_by(adm2_pcode, adm2_name, adm1_name, adm0_name, year_month, event_type) |>
  summarise(disaster = max(disaster), .groups = "drop")

# Complete month spine: every month from 2018-01 to today
all_months  <- format(seq(as.Date("2018-01-01"), Sys.Date(), by = "month"), "%Y-%m")
admin_units <- result_spatial |> distinct(adm2_pcode, adm2_name, adm1_name, adm0_name)
event_types <- c("EQ", "TC")

spine <- crossing(
  admin_units,
  year_month = all_months,
  event_type = event_types
)

# Join spine to results — months with no event get disaster = 0
result_panel <- spine |>
  left_join(result_monthly, by = c("adm2_pcode", "adm2_name", "adm1_name",
                                   "adm0_name", "year_month", "event_type")) |>
  mutate(disaster = replace_na(disaster, 0L))

message(sprintf("✔ Panel rows: %d  (%d admin units × %d months × %d event types)",
                nrow(result_panel), nrow(admin_units), length(all_months), length(event_types)))

# =============================================================================
# PART 5: SAVE OUTPUTS
# =============================================================================

output_dir <- "gdacs_results"
if (!dir.exists(output_dir)) dir.create(output_dir, recursive = TRUE)

panel_long  <- file.path(output_dir, "gdacs_panel_long.csv")
panel_wide  <- file.path(output_dir, "gdacs_panel_wide.csv")

write.csv(result_panel, panel_long, row.names = FALSE)

result_panel_wide <- result_panel |>
  pivot_wider(names_from  = event_type,
              values_from = disaster,
              names_prefix = "disaster_")

write.csv(result_panel_wide, panel_wide, row.names = FALSE)

message(sprintf("Saved: %s and %s", panel_long, panel_wide))
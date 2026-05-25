# Merge per-country ADM2 GeoJSONs into countries_of_interest.geojson.
# Run from repo root:
#   Rscript country_adm2_shapefiles/shapefiles_combination.R

library(sf)
library(dplyr)

shape_dir <- "country_adm2_shapefiles"
if (!dir.exists(shape_dir)) {
  stop("Expected directory not found: ", shape_dir, "\n  Run this script from the repository root.")
}

setwd(shape_dir)

columns <- c("adm2_name", "adm0_name", "adm2_pcode", "adm1_name", "geometry")
file_list <- list.files(path = ".", pattern = "_admin2.*\\.geojson$", full.names = FALSE)

if (length(file_list) == 0) {
  stop("No per-country *_admin2*.geojson files found in ", getwd())
}

message(sprintf("Merging %d boundary files...", length(file_list)))

all_shapes <- lapply(file_list, st_read, quiet = TRUE)
all_shapes_selected <- lapply(all_shapes, function(x) {
  st_as_sf(x %>% dplyr::select(all_of(columns)))
})

merged <- do.call(rbind, all_shapes_selected)
out_path <- "countries_of_interest.geojson"
st_write(merged, out_path, delete_dsn = TRUE, quiet = TRUE)
message(sprintf("Wrote %s (%d features)", out_path, nrow(merged)))

dash = c(13.8, 14.1, 15.7, 14.5, 13.3, 14.9, 15.1, 14.0)

dash_mean = mean(dash)
sum_sq_diff = 0

for (x in dash) {
  squared_diff = (x - dash_mean)^2
  sum_sq_diff = sum_sq_diff + squared_diff
}

var_dash = sum_sq_diff / (length(dash)-1)
sqrt_dash = sqrt(var_dash)
print(var_dash)


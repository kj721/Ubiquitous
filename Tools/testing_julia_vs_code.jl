using QuadGK

# Define the function
f(x) = x^3 - 2x^2 - 4x + 1

# Calculate the definite integral from 2 to 5
integral, error = quadgk(f, 2, 5)

# Output the result
println("The definite integral from 2 to 5 is: ", integral)
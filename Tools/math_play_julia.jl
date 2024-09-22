println("Testing 1-2-3")
1 + 8
98 ^ 34

x = 21
y = 34
z = 28

(x + y) ^ z

chibbert = (x ^ z) / yield
println(chibbert)

using QuadGK

# Define the function
f(x) = x^3 - 2x^2 - 4x + 1

# Calculate the definite integral from 2 to 5
integral, error = quadgk(f, 2, 5)

# Output the result
println("The definite integral from 2 to 5 is: ", integral)

using SymPy

# Define the symbolic variables
x = symbols("x")

# Define a quadratic expression, e.g., (a*x + b)*(c*x + d)
a, b, c, d = symbols("a b c d")
quadratic_expr = (a*x + b)*(c*x + d)

# Expand the quadratic expression
expanded_expr = expand(quadratic_expr)

println(expanded_expr)

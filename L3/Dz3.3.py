a = [1, 2, 3, 4, 5]
n = len(a)
if n == 0:
    result = [[], []]

else:

    half = n // 2
    print(n)
    print(half)
    if n - (half * 2) == 1:
        half = half + 1
    v = a[:half]
    m = a[half:]

    result = [v, m]
print(result)

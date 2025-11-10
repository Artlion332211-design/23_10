a = [0, 1, 7, 2, 4, 8]
if len(a) == 0:
    result = 0
else:
    s = 0
    for i in range(0, len(a), 2):
        s = s + a[i]
    last = a[-1]
    result = s * last

print(result)

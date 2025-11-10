a = [0, 1, 0, 12, 3]

b = []
c = []

for number in a:
    if number == 0:
        c.append(number)
    else:
        b.append(number)

result = b + c

print(result)

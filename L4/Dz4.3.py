import random
n = random.randint(3, 10)
a = []
for _ in range(n):
    a.append(random.randint(0, 9))
b = [a[0], a[2], a[-2]]

print("Початковий список:", a)
print("Фінальний список :", b)

n = int(input("Введи 4-значне число: "))
a, b = divmod(n,10)
#print(a) #123
print(b) #4
a, b = divmod(a,10)
#print(a)
print(b)
a, b = divmod(a,10)
print(b)
print(a)
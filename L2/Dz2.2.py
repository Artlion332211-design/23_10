n =int(input("Введи 5-значне число: "))
a,b = divmod(n,10)
z=b
#print(b)
#print(a,b)
a,b = divmod(a,10)
#print(a,b)
z=z * 10 + b
#print(z)
a,b = divmod(a,10)
z=z * 10 + b
a,b = divmod(a,10)
z=z * 10 + b
z = z * 10 + a

print(z)
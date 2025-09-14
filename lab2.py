import hashlib

hash_object = input("Введите строку с вашим иин: ")

Invalid_zero_count = True

while Invalid_zero_count:
    Zero_max = int(input("Введите до скольки 0 вы хотите искать, от 1 до 5:"))
    if 1 <= Zero_max <= 5:
        Invalid_zero_count = False

for k in range(1, Zero_max + 1):
    prefix = "0" * k
    attempts =  0 # количество попыток
    number = 0 

while True:
    final_hash_object = hash_object + "+" + str(number)
    hash_result = hashlib.sha256(final_hash_object.encode("utf-8")).hexdigest()
    attempts += 1

    if hash_result.startswith(prefix):
        print(f"k={k} | attempts={attempts} | nonce={number} | input={final_hash_object} | sha256={hash_result}")
        break                       # нашли для этого k -> выходим из while и пойдём к k+1
    else:
        number += 1  
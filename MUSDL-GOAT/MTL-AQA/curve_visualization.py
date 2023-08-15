import os
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

log_path = 'exp1'
file_name = 'USDL_full_split3_1e-05_Mon Aug 14 16 05 35 2023.log'
state = 'test'
best_ep = 153
f_path = os.path.join(log_path, file_name)
loss_list = []

# show plt of rho
with open(f_path, 'r') as f:
    for line in f:
        if line.startswith('epoch') and line.split(',')[1].split(' ')[1] == state:
            loss_list.append(float(line.split(':')[-1]))
    f.close()
s = pd.Series(loss_list, name='rho')
sns.lineplot(data=s)
title = state + ',' + file_name.split('_')[3]
plt.title(title)
plt.show()
plt.savefig('curve.png')

# calculate mean and std
if state == 'test':
    adjacent_five = loss_list[best_ep - 2:best_ep + 3]
    print(adjacent_five)
    print('mean:', np.mean(adjacent_five))
    print('std:', np.std(adjacent_five))
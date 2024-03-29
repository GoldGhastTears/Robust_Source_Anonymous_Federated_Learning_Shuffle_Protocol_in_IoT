import os
import sys
import argparse
import copy
from tqdm import tqdm
import random
import secrets
import numpy as np
import torch
import torch.nn.functional as F
from torch import optim
from sympy import isprime, nextprime
from Models import Mnist_2NN, Mnist_CNN

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter, description="FedAvg")
parser.add_argument('-g', '--gpu', type=str, default='0', help='gpu id to use(e.g. 0,1,2,3)')
parser.add_argument('-np', '--num_of_participants', type=int, default=20, help='numer of the clients')
parser.add_argument('-kp', '--k_positions', type=int, default=2, help='number of positions that each participant can choose')

parser.add_argument('-cf', '--cfraction', type=float, default=0.9, help='C fraction, 0 means 1 client, 1 means total clients')
parser.add_argument('-E', '--epoch', type=int, default=5, help='local train epoch')
parser.add_argument('-B', '--batchsize', type=int, default=10, help='local train batch size')
parser.add_argument('-mn', '--model_name', type=str, default='mnist_2nn', help='the model to train')
parser.add_argument('-lr', "--learning_rate", type=float, default=0.01, help="learning rate, \
                    use value from origin paper as default")
parser.add_argument('-vf', "--val_freq", type=int, default=5, help="model validation frequency(of communications)")
parser.add_argument('-sf', '--save_freq', type=int, default=20, help='global model save frequency(of communication)')
parser.add_argument('-ncomm', '--num_comm', type=int, default=1000, help='number of communications')
parser.add_argument('-dr', '--drop_rate', type=float, default=0.3, help='drop rate')
parser.add_argument('-t', '--threshold', type=float, default=5, help='the minimum number of hosts that can complete the iteration')

parser.add_argument('-sp', '--save_path', type=str, default='./checkpoints', help='the saving path of checkpoints')
parser.add_argument('-iid', '--IID', type=int, default=0, help='the way to allocate data to clients')



def test_mkdir(path):
    if not os.path.isdir(path):
        os.mkdir(path)

def generate_params():
    binary_operator = "+"
    #binary_operator = "*"

    random_bytes = secrets.token_bytes(6)
    random_number = int.from_bytes(random_bytes, byteorder='big')
    p = nextprime(random_number)  # a large prime number

    #a = random.randint(1, p)
    a = random.randint(1, int(str(p)[:4]))

    g = random.randint(2, 10)  # generator

    if binary_operator == '+':
        G = list(set(range(0, 100, g)))
    if binary_operator == '*':
        G = [g**i for i in range(10)]

    h = random.choice(G)

    return {"G": G, "g": g, "h": h, "p": p, "a": a, "b": binary_operator}

def simulate_offline(all_clients_in_comm, drop_rate):
    random.shuffle(all_clients_in_comm)
    num_to_remove = int(len(all_clients_in_comm) * drop_rate)
    shuffled_and_removed = all_clients_in_comm[:-num_to_remove]

    return shuffled_and_removed


if __name__ == "__main__":
    args = parser.parse_args()
    args = args.__dict__

    test_mkdir(args['save_path'])

    os.environ['CUDA_VISIBLE_DEVICES'] = args['gpu']
    dev = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    #dev = torch.device("mps") if (torch.backends.mps.is_available() and torch.backends.mps.is_built()) else dev


    net = None
    if args['model_name'] == 'mnist_2nn':
        net = Mnist_2NN()
    elif args['model_name'] == 'mnist_cnn':
        net = Mnist_CNN()

    if torch.cuda.device_count() > 1:
        print("Let's use", torch.cuda.device_count(), "GPUs!")
        net = torch.nn.DataParallel(net)
    net = net.to(dev)

    loss_func = F.cross_entropy
    opti = optim.SGD(net.parameters(), lr=args['learning_rate'])

    # for clients.py
    param = generate_params()
    print("===== Params generation completed =====")

    Np = int(max(args['num_of_participants'] * args['cfraction'], 1))  # number in communication
    data_positions = list(range(1, args['k_positions'] * Np + 1))
    random.shuffle(data_positions)

    private_key = random.randint(1, int(str(param['p'])[:4]))
    if param["b"] == "+":
        public_key = param['g'] * private_key
    elif param["b"] == "*":
        #public_key = param['g'] ** private_key
        pass

    from clients import ClientsGroup, Clients, bilinear_pairing_function
    Clients.param = param
    Clients.k_positions = args['k_positions']

    myClients = ClientsGroup('mnist', args['IID'], args['num_of_participants'], dev)
    testDataLoader = myClients.test_data_loader
    clients_set = myClients.get_clients()
    Clients.clients_set = clients_set
    print("===== Clients generation completed =====\n")


    global_parameters = {}
    for key, var in net.state_dict().items(): # 将net中的参数保存在字典中（是参数，不是训练梯度）
        # key,value格式例子：
        # conv1.weight 	 torch.Size([6, 3, 5, 5])
        # conv1.bias 	 torch.Size([6])
        # conv2.weight 	 torch.Size([16, 6, 5, 5])
        # conv2.bias 	 torch.Size([16])
        # fc1.weight 	 torch.Size([120, 400])
        # fc1.bias 	     torch.Size([120])
        # fc2.weight 	 torch.Size([84, 120])
        # fc2.bias 	     torch.Size([84])

        # .state_dict() 将每一层与它的对应参数建立映射关系
        # .item() 取出tensor中的值，变为Python的数据类型
        global_parameters[key] = var.clone()  # clone原来的参数，并且支持梯度回溯


    for comm_round in range(args['num_comm']):
        print("== Communicate round {} ==".format(comm_round + 1))

        order = np.random.permutation(args['num_of_participants']) # Shuffle the clients
        clients_in_comm = ['client{}'.format(comm_round) for comm_round in order[0:Np]]
        Clients.clients_in_comm = clients_in_comm # Send to all clients

        '''=====数据位置生成阶段====='''
        # Round 1
        Pi = clients_in_comm[0]
        myClients.round1(Pi)

        '''for each_client in clients_in_comm:
            print(myClients.clients_set[each_client].request_parameters)
'''
        # Round 2
        for each_client in clients_in_comm:
            token, verification_information, amount_of_request_parameters = \
                myClients.clients_set[each_client].get_token_and_verification_information()

            k_plus_Np = args['k_positions'] * len(clients_in_comm)
            temp_exponent = param['a'] * (k_plus_Np - len(myClients.clients_set[each_client].request_parameters))

            left_side = bilinear_pairing_function(token, verification_information)

            if param["b"] == "+":
                right_side = bilinear_pairing_function(param['g'], param['h'] * temp_exponent)
            elif param["b"] == "*":
                right_side = bilinear_pairing_function(param['g'], param['h'] ** temp_exponent)

            if left_side != right_side: # 双线性配对函数 bilinear pairing function
                print("===== Agreement terminated 1=====")
                sys.exit(1)
            else:
                #random_mask = random.randint(1, param['p'])
                random_mask = random.randint(1, int(str(param['p'])[:4]))
                # OT.Enc
                secret_list = []
                for count in range(args['k_positions'] * Np): # count == n
                    if count == 0:
                        # C0
                        if param["b"] == "+":
                            secret_list.append(token * random_mask)
                        elif param["b"] == "*":
                            secret_list.append(token ** random_mask)
                    else:
                        # Cn
                        if param["b"] == "+":
                            secret_list.append(bilinear_pairing_function(param['g'] * (1 / (param['a'] + count)),
                                                                         param['h'] * random_mask) * data_positions[count])
                        elif param["b"] == "*":
                            secret_list.append(bilinear_pairing_function(param['g'] ** (1 / (param['a'] + count)),
                                                                         param['h'] ** random_mask) * data_positions[count])

                myClients.clients_set[each_client].set_secret_list(secret_list)
                myClients.clients_set[each_client].decrypt_secret()

        '''=====数据匿名收集阶段====='''
        # Round 1
        u1 = clients_in_comm
        #random.shuffle(u1)
        for client in u1:
            local_parameters = myClients.clients_set[client].local_update(args['epoch'], args['batchsize'], net,
                                                                         loss_func, opti, global_parameters)

            myClients.clients_set[client].generate_anonymous_model_upload_list(global_parameters, local_parameters)
            myClients.clients_set[client].generate_and_encrypt_shared_values(args['threshold'])

        u2 = simulate_offline(u1, args['drop_rate'])
        if len(u2) < args['threshold']:
            print("===== Agreement terminated 2=====")
            sys.exit(1)
        else:
            all_anonymous_model_upload_list = []
            all_encrypted_shared_values = []
            for client in u2:
                anonymous_model_upload_list = myClients.clients_set[client].get_anonymous_model_upload_list()
                all_anonymous_model_upload_list.append(anonymous_model_upload_list)

                encrypted_shared_values = myClients.clients_set[client].get_encrypted_shared_values()
                all_encrypted_shared_values.append(encrypted_shared_values)

            for client in u2:
                decryptable_shared_values = []
                for each_dict in all_encrypted_shared_values:
                    decryptable_shared_values.append(each_dict[client])
                myClients.clients_set[client].receive_decryptable_shared_values(decryptable_shared_values)


        u3 = simulate_offline(u2, args['drop_rate'])
        if len(u3) < args['threshold']:
            print("===== Agreement terminated 3=====")
            sys.exit(1)
        else:
            # Round 2
            summed_values_dict = {}
            for client in u3:
                summed_shared_values = myClients.clients_set[client].decrypt_and_sum_shared_values()
                summed_values_dict[client] = summed_shared_values


            aggregation_model_list = copy.deepcopy(all_anonymous_model_upload_list[0])
            for each_model_upload_list in all_anonymous_model_upload_list[1:]:
                for each_gradient in range(args['k_positions'] * Np):
                    for key, var in each_model_upload_list[each_gradient].items():
                        if param["b"] == "+":
                            aggregation_model_list[each_gradient][key] += var.clone()
                        elif param["b"] == "*":
                            aggregation_model_list[each_gradient][key] *= var.clone()


            new_aggregation_model_list = [] # Lw

            # part 2
            part_2 = {}
            for layer, model_parameter in global_parameters.items():
                part_2[layer] = model_parameter.clone() ** (len(u2))

            for item_count in range(1, args['k_positions'] * Np + 1):
                # part 1
                temp_sum = 0
                for client in u2:
                    temp_sum += myClients.clients_set[client].model_mask
                if param["b"] == "+":
                    part_1 = param['g'] * (temp_sum + len(u2) * item_count)
                elif param["b"] == "*":
                    part_1 = param['g'] ** (temp_sum + len(u2) * item_count)

                temp_dict = {}
                for layer in part_2.keys():
                    product = part_1 * part_2[layer].clone()# * part_3[layer].clone()
                    temp_dict[layer] = product.clone()

                new_aggregation_model_list.append(temp_dict)


            def convert_to_num(str):
                return int(str[6:]) + 1

            sum_of_secrets = 0
            for i_keys in summed_values_dict.keys():
                i = convert_to_num(i_keys)
                product_of_secrets = 1
                for j_keys in u2:
                    j = convert_to_num(j_keys)
                    if i != j:
                        product_in_j = (-j / (i - j)) * summed_values_dict[i_keys]
                        product_of_secrets *= product_in_j
                sum_of_secrets += product_of_secrets














            summed_model_mask = 0
            for client in u2:
                summed_model_mask += myClients.clients_set[client].model_mask

            if param["b"] == "+":
                original_model_gradient_list = []

                for item_count in range(1, args['k_positions'] * Np + 1):

                    temp_denominator = {}
                    for key, var in global_parameters.items():
                        temp_denominator[key] = var.clone() * param["g"] * (summed_model_mask + len(u2) * item_count)

                    temp_dict = {}
                    for key in temp_denominator.keys():
                        A=aggregation_model_list[item_count - 1][key].clone()
                        B=temp_denominator[key]
                        C=global_parameters[key].clone()
                        temp_dict[key] = A - B - C
                    original_model_gradient_list.append(temp_dict)

                    # check_whether_it_is_zero = []
                    # for var in temp_dict.values():
                    #     is_within_range = (var >= 0.9) & (var <= 1.1)
                    #     if is_within_range.all().item():
                    #         check_whether_it_is_zero.append(True)
                    #     else:
                    #         check_whether_it_is_zero.append(False)
                    # if (False in check_whether_it_is_zero):
                    #     original_model_gradient_list.append(temp_dict)
                    # else:
                    #     original_model_gradient_list.append(0)



            elif param["b"] == "*":
                original_model_gradient_list = []

                # the right part of the denominator (constant
                temp_denominator_right = {}
                for key, var in global_parameters.items():
                    temp_denominator_right[key] = var.clone() ** (len(u2) - 1)


                for item_count in range(1, args['k_positions'] * Np + 1):

                    # the left part of the denominator
                    temp_exponent = summed_model_mask + item_count * len(u2)

                    temp_denominator_left = param['g'] ** temp_exponent

                    # each item in original_model_gradient_list is a model parameters with layers
                    temp_dict = {}
                    for key in temp_denominator_right.keys():
                        # temp_dict[key] = aggregation_model_list[item_count - 1][key].clone() \
                        #                    / (temp_denominator_left * temp_denominator_right[key].clone()) - global_parameters[key].clone()

                        A = aggregation_model_list[item_count - 1][key].clone()
                        B = temp_denominator_left
                        C = temp_denominator_right[key].clone()
                        D = global_parameters[key].clone()
                        temp_dict[key] = A / (B * C) - D

                    original_model_gradient_list.append(temp_dict)













            print("0：{}".format(original_model_gradient_list.count(0)))
            print("非空：{}".format(len(original_model_gradient_list) - original_model_gradient_list.count(0)))
            print("u2：{}".format(len(u2)))
            if 0 in original_model_gradient_list: # to make sure 0 is in the list, to avoid error
                if len(original_model_gradient_list) - original_model_gradient_list.count(0) == len(u2):
                    pass
                else:
                    pass
                    #print("===== Agreement terminated 4 =====")
                    #sys.exit(1)

            for key in temp_denominator.keys():
                for each_parameter in original_model_gradient_list:
                    if each_parameter != 0:
                        global_parameters[key] += each_parameter[key]
                number_of_zero_in_original_model_gradient_list = len(original_model_gradient_list)\
                                                                 - original_model_gradient_list.count(0)
                global_parameters[key] = global_parameters[key] / number_of_zero_in_original_model_gradient_list


        '''sum_parameters = None
        for client in tqdm(clients_in_comm):

            local_parameters = myClients.clients_set[client].local_update(args['epoch'], args['batchsize'], net,
                                                                         loss_func, opti, global_parameters)
            if sum_parameters is None: # First iteration
                sum_parameters = {}
                for key, var in local_parameters.items():
                    sum_parameters[key] = var.clone()
            else: # Not first iteration
                for var in sum_parameters:
                    sum_parameters[var] = sum_parameters[var] + local_parameters[var]

        for key in global_parameters:
            global_parameters[key] = (sum_parameters[key] / Np)'''

        with torch.no_grad():
            #if (comm_round + 1) % args['val_freq'] == 0:
            if True:
                net.load_state_dict(global_parameters, strict=True)
                sum_accu = 0
                num = 0
                for data, label in testDataLoader:
                    data, label = data.to(dev), label.to(dev)
                    preds = net(data)
                    preds = torch.argmax(preds, dim=1)
                    sum_accu += (preds == label).float().mean()
                    num += 1
                print('accuracy: {}'.format(sum_accu / num))


    if 0:
       if (comm_round + 1) % args['save_freq'] == 0:
            torch.save(net, os.path.join(args['save_path'],
                                         '{}_num_comm{}_E{}_B{}_lr{}_num_clients{}_cf{}'.format(args['model_name'],
                                                                                                comm_round, args['epoch'],
                                                                                                args['batchsize'],
                                                                                                args['learning_rate'],
                                                                                                args['num_of_participants'],
                                                                                                args['cfraction'])))


